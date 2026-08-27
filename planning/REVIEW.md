# Change Review

## 2026-08-27 — Market data design documents

**Reviewer note:** the project convention in `.claude/agents/change-reviewer.md`
is to delegate this to `codex` as an independent reviewer. `codex exec` failed
with `You've hit your usage limit ... try again at Sep 16th, 2026`, so this is
a **self-review by the same agent that authored the changes** and does not
satisfy the independence requirement. Re-run the codex review when the quota
resets.

**Scope:** three untracked files, no tracked files modified.

```
?? planning/MARKET_INTERFACE.md
?? planning/MARKET_SIMULATOR.md
?? planning/MASSIVE_API.md
```

These are design documents, so "correctness" here means the specified code
would work as described and the three documents agree with each other and
with `PLAN.md`. Two defects below were reproduced by executing the code.

---

### 1. Grouped-daily fallback can issue 5 API calls per fetch — High

`MARKET_INTERFACE.md` §6 (`_fetch_grouped`) and `MASSIVE_API.md` §6.1
(`fetch_grouped_daily`) both loop `for offset in range(5)`, calling the API
once per candidate date until one returns data.

Both documents state the opposite invariant:

- `MARKET_INTERFACE.md` §6: "One HTTP call per fetch, in either mode."
- `MASSIVE_API.md` §5: every row of the endpoint table claims 1 call per poll.

On a Monday the loop burns a call on Monday (not yet published on an EOD
plan), Sunday and Saturday before reaching Friday's bar — four calls. The
free tier allows five per minute, so a single poll can nearly exhaust the
budget, and the next poll 15 seconds later gets a 429.

This matters precisely because grouped-daily *is* the free-tier path, which
is where the rate limit binds.

Fix: resolve the trading date once and cache it, re-resolving only when the
cached date's data goes stale.

```python
async def _fetch_grouped(self, tickers):
    if self._session_date is None:
        self._session_date = await self._resolve_session_date()
    rows = await self._grouped_bars(self._session_date)
    ...
```

### 2. The contract test for unknown tickers cannot pass for both providers — High

`MARKET_INTERFACE.md` §10:

```python
async def test_unknown_ticker_is_omitted_not_raised(provider):
    quotes = await provider.fetch(["NOTATICKER"])
    assert "NOTATICKER" not in quotes
```

Reproduced against the simulator from `MARKET_SIMULATOR.md` §7:

```
simulator fetch(['NOTATICKER']) -> {'NOTATICKER': (172.47, 172.22)}
assertion passes for simulator: False
```

This is not just a bad test. The two documents specify contradictory
behaviour for the same interface method:

- `MARKET_INTERFACE.md` §4: "Tickers with no available price are omitted."
- `MARKET_SIMULATOR.md` §6: "the simulator must price anything", via
  `derive_spec`.

The simulator never omits; Massive omits anything unrecognised. So the
abstract contract as written is not one both implementations honour, which
undercuts the stated goal of provider-agnostic downstream code — a watchlist
add of a typo'd symbol succeeds silently under the simulator and fails under
Massive.

Decide which is the contract and make both documents say it. The simulator's
behaviour is the more useful one for a demo, so the likely resolution is to
have `MassiveProvider` validate unknown symbols at watchlist-add time
(`/v2/aggs/ticker/{t}/prev`, free on all plans) and drop this from the shared
contract suite into provider-specific tests.

### 3. The session-rollover test is flaky — Medium

`MARKET_SIMULATOR.md` §10:

```python
assert quotes["AAPL"].previous_close == pytest.approx(quotes["AAPL"].price, abs=0.01)
```

`_advance` sets `closes` from `prices` *before* stepping, so after the
rollover tick the price is already one step away from the close. Measured
across 60 seeds:

```
mean |price - prev_close| 0.127, max 0.420
seeds passing abs=0.01: 6/60
```

It passed in the authoring run only because `seed=4` happened to draw a small
move. The "Measured behaviour" table added to §10 is therefore built partly
on a test that fails 90% of the time.

Fix: assert the property that actually matters — that the close moved off the
seed price — rather than a tolerance that encodes one lucky draw.

```python
assert quotes["AAPL"].previous_close != 190.00
assert quotes["AAPL"].previous_close == pytest.approx(quotes["AAPL"].price, abs=1.0)
```

### 4. `previous_close` carries two different meanings — Medium

`MARKET_INTERFACE.md` §6 `_fetch_grouped` maps `previous_close` to `row["o"]`,
the session *open*, while snapshot mode maps it to `prevDay.c`, the prior
session's close. The document acknowledges this ("the honest reading of the
only data a free key has") but leaves one field meaning two things depending
on a mode the caller cannot see.

Downstream, `Quote.change_percent` feeds the watchlist's daily-change column,
so the same UI element silently switches from "change vs prior close" to
"change across that session". Either rename to reflect the reference point,
or carry the reference explicitly so the frontend can label it.

### 5. `PriceCache.prune` is never called — Low

Defined in `MARKET_INTERFACE.md` §7, invoked nowhere in any of the three
documents. `CLAUDE.md` says not to overengineer. Either wire it to watchlist
removal or delete it.

### 6. Poll cadence drifts by the fetch duration — Low

`PriceEngine._run` sleeps `poll_interval` *after* the fetch, so the true
period is `fetch_time + poll_interval`. Negligible for the simulator,
material for Massive, where a slow response pushes a nominal 15s poll toward
16-17s. Not a correctness bug, but the free-tier budgeting in
`MASSIVE_API.md` §3 assumes a fixed cadence. Worth a sentence, or use a
deadline-based sleep.

### 7. Test dependencies unstated — Low

The suites in `MARKET_INTERFACE.md` §10 and `MARKET_SIMULATOR.md` §10 use
bare `async def` tests and an `httpx_mock` fixture, which need
`pytest-asyncio` (with `asyncio_mode = "auto"`) and `pytest-httpx`. Neither is
mentioned. `statistics.correlation` also requires Python 3.10+, satisfied by
`PLAN.md`'s 3.12 but worth pinning.

### 8. One unverified claim — Low

`MASSIVE_API.md` §4.3 states Basic plans "see a delay of roughly one day
before a session's grouped bar is published". This was inferred from the
end-of-day recency tier, not confirmed in the documentation or against a live
free key. It should be marked as inferred or verified before anyone plans
around it. Everything else in that document was checked against
`massive.com/docs` or the live API.

---

### Holding up well

- The free-tier findings are the substantive result: snapshot endpoints
  return 403 on Stocks Basic, and grouped-daily is the one batch endpoint
  that is free. This corrects `PLAN.md` §6, which assumed snapshot access on
  the free tier.
- Auth methods, error bodies and the `api.massive.com` base URL were
  confirmed against the live API rather than copied from documentation.
- The simulator's core math verifies: realized volatility 0.2804 against a
  0.28 target, correlations 0.597/0.355 against 0.60/0.35. The Ito correction
  and the `dt` trading-calendar conversion are both right, which is where
  this usually goes wrong.
- The two determinism traps called out in `MARKET_SIMULATOR.md` §8 (unsorted
  set iteration, built-in `hash()`) are real and easy to trip.

### Verdict

No blocking issues for documents that have not been implemented yet, but
findings 1 and 2 are specification defects rather than typos: an
implementer following these documents literally would write a rate-limit bug
and a contract test that cannot pass. Both should be resolved before the
backend agent picks this up. Findings 3-8 can be folded in at the same time.

The independence requirement was not met; re-run the codex review when its
quota resets.

---

## 2026-08-27 — Independent review re-attempt

`codex exec` was re-run per `.claude/agents/change-reviewer.md` and failed
again with the same quota error:

```
ERROR: You've hit your usage limit. ... try again at Sep 16th, 2026 9:00 PM.
```

The independence requirement therefore still stands unmet. The findings above
were re-checked against the current file contents and all still apply — none
have been fixed:

| Finding | Check | Status |
|---|---|---|
| 1 | `for offset in range(5)` at `MARKET_INTERFACE.md:316`, `MASSIVE_API.md:385`; "One HTTP call per fetch" still at `MARKET_INTERFACE.md:360` | open |
| 2 | `MARKET_INTERFACE.md:168` says omitted; `MARKET_SIMULATOR.md:315` still `UNIVERSE.get(ticker) or derive_spec(ticker)` | open |
| 3 | `abs=0.01` still at `MARKET_SIMULATOR.md:497` | open |
| 5 | `prune` still defined at `MARKET_INTERFACE.md:426`, called nowhere | open |

Re-run the codex review after Sep 16 2026 for genuine independence.
