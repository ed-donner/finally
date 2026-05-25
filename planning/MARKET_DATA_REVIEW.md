# Market Data Backend — Code Review

**Date:** 2026-05-25
**Reviewer:** Coding Agent (comprehensive review)
**Scope:** `backend/app/market/` (8 source modules) and `backend/tests/market/` (6 test modules)
**Environment:** Python 3.13.7, `uv` project synced from lockfile, `massive` 2.2.0 installed

---

## 1. Executive Summary

The market data subsystem is well-architected and the **simulator path (the default) is solid and production-ready**. The code follows a clean strategy pattern, the GBM math is correct, the price cache is properly synchronized, and the SSE stream emits the documented format.

However, this review found **one high-severity runtime bug in the Massive (real market data) path**: it is non-functional against the real `massive` package and silently produces no data. The bug is masked by the test suite because the Massive tests mock the snapshot object with `MagicMock`, which fabricates any attribute accessed. All 73 tests pass, but they do not exercise the real data contract.

| Check | Result |
|---|---|
| Unit/integration tests | **73 passed, 0 failed** |
| Coverage (overall) | **91%** (up from 84% in the prior review) |
| Lint (`ruff check app/ tests/`) | **Clean** |
| Deprecation warnings (`-W error::DeprecationWarning`) | **None** |
| Simulator path | ✅ Verified working end-to-end |
| Massive path | ❌ **Broken at runtime** (see Finding H-1) |
| SSE stream format | ✅ Verified manually (matches spec) |

---

## 2. Test Results

```
73 passed in ~1.3–2.0s
```

Per-module coverage:

| Module | Coverage | Notes |
|---|---|---|
| models.py | 100% | |
| cache.py | 100% | |
| interface.py | 100% | |
| factory.py | 100% | |
| seed_prices.py | 100% | |
| simulator.py | 98% | Uncovered: dup-guard L149, exception log L268–269 |
| massive_client.py | 94% | Uncovered: poll-loop sleep L85–87, `_fetch_snapshots` L125 |
| stream.py | 33% | No automated SSE test (verified manually instead) |
| **TOTAL** | **91%** | |

**Continuity with the prior review (`planning/archive/MARKET_DATA_REVIEW.md`):** the high-severity `pyproject.toml` build-config bug is **fixed** (`[tool.hatch.build.targets.wheel] packages = ["app"]` present; `uv sync` succeeds). The five previously-failing Massive tests now pass because the `massive` package is installed. Unused test imports and the `_generate_events` return annotation are also resolved.

---

## 3. Verification Performed Beyond the Test Suite

Because the suite mocks the external API and does not cover the SSE generator, I ran targeted checks:

1. **Massive package API contract** — `RESTClient`, `SnapshotMarketType`, and `get_snapshot_all(market_type, tickers=...)` all exist in `massive` 2.2.0 with matching signatures. ✅
2. **Real snapshot shape** — inspected `TickerSnapshot`/`LastTrade.from_dict`. This surfaced Finding H-1 below. ❌
3. **Full 10-ticker Cholesky** — correlation matrix for the default watchlist is positive-definite (min eigenvalue ≈ 0.40); decomposition succeeds. ✅
4. **Numerical stability** — 5,000 simulator steps keep every price positive and finite. ✅
5. **SSE generator output** — emits `retry: 1000\n\n` then `data: {…}` frames containing all documented fields (`ticker, price, previous_price, timestamp, change, change_percent, direction`); honors `is_disconnected()`. ✅

---

## 4. Findings

### H-1 — Massive path is broken: wrong `last_trade` timestamp attribute (Severity: High) — ✅ FIXED 2026-05-25

`massive_client.py:103` reads:

```python
timestamp = snap.last_trade.timestamp / 1000.0
```

The real `massive` 2.2.0 `LastTrade` model has **no `timestamp` attribute**. Its timestamp fields are `sip_timestamp`, `participant_timestamp`, and `trf_timestamp` (the snapshot JSON `lastTrade.t` maps to `sip_timestamp`). Accessing `.timestamp` raises `AttributeError`, which `_poll_once` catches per-snapshot and logs as a warning:

```
Skipping snapshot for AAPL: 'LastTrade' object has no attribute 'timestamp'
```

**Consequence:** with a real `MASSIVE_API_KEY` set, **every** snapshot is skipped on **every** poll, so the price cache is never populated and the frontend receives an empty SSE stream. The defensive `except (AttributeError, TypeError)` turns a hard crash into a silent, total failure of the real-data feature.

**Why the tests don't catch it:** `tests/market/test_massive.py` builds snapshots with `MagicMock`, and `MagicMock` auto-creates a `.timestamp` attribute on access. Reproduced with a realistically-shaped object:

```python
snap = TickerSnapshot.from_dict({"ticker": "AAPL",
    "lastTrade": {"p": 190.50, "t": 1707580800000000000}})
# -> _poll_once skips it; cache.get_price("AAPL") is None
```

**Fix applied** (`massive_client.py`): the timestamp is now read from `sip_timestamp` (Unix nanoseconds → seconds) via a small helper that returns `None` when the field is absent, in which case `PriceCache.update` falls back to wall-clock time rather than dropping the snapshot:

```python
@staticmethod
def _trade_timestamp_seconds(last_trade) -> float | None:
    ts_ns = getattr(last_trade, "sip_timestamp", None)
    if ts_ns is None:
        return None
    return ts_ns / 1e9
```

The previous `/ 1000.0` was also wrong by ~10⁶ (ns, not ms). The unit should still be confirmed against live API data when a real key is available.

### M-1 — Massive tests assert against a fictional object shape (Severity: Medium) — ✅ FIXED 2026-05-25

The root cause of H-1 going undetected was that the Massive tests mocked with `MagicMock`, which fabricates any attribute accessed. **Resolved:** the mock helper now sets the real field name (`sip_timestamp`), and a new regression test `test_real_snapshot_shape_populates_cache` builds a snapshot via `TickerSnapshot.from_dict({...})` (real JSON shape) and asserts the cache is populated. Verified that this test **fails against the pre-fix code** (`AttributeError: 'LastTrade' object has no attribute 'timestamp'`) and passes after the fix, so H-1 can no longer regress silently.

### L-1 — `PriceCache.version` read without the lock (Severity: Low)

```python
@property
def version(self) -> int:
    return self._version
```

Reading a single `int` is atomic under CPython's GIL, so this is safe today. It is inconsistent with the rest of the class and could race on a free-threaded build (PEP 703, 3.13t+). Carried over from the prior review; still present.

### L-2 — Module-level `router` + closure registration (Severity: Low)

`stream.py:17` defines a module-level `router`, and `create_stream_router()` registers `/prices` on it via closure. Calling the factory twice (e.g., in a test) registers the route twice on the same shared router. Harmless in the single-startup production flow, but a latent footgun. Prefer creating a fresh `APIRouter()` inside the factory.

### L-3 — No SSE keepalive between cache changes (Severity: Low)

`_generate_events` only yields when `price_cache.version` changes. With the simulator (500ms ticks) the version always changes, so this is moot. With the Massive poller (15s interval) there can be ~15s gaps with no bytes sent; some intermediary proxies close idle connections. A periodic comment heartbeat (`: keepalive\n\n`) every ~10–15s would make the stream robust independent of data source cadence.

### L-4 — `stream.py` has no automated test (Severity: Low)

At 33% line coverage, the SSE endpoint is the only module without dedicated tests. I verified the generator manually (Section 3), but an ASGI-client test (`httpx.ASGITransport`) reading a couple of frames would guard the format and the disconnect path going forward.

### N-1 — Loose `massive` version floor (Severity: Nitpick)

`pyproject.toml` declares `massive>=1.0.0` but the resolved/installed version is 2.2.0. The code depends on the 2.x `LastTrade`/`TickerSnapshot` shape. The lockfile pins the working version, but the loose floor means a 1.x resolution could differ. Consider tightening to `massive>=2,<3` once H-1 is fixed and the contract is pinned by a test.

---

## 5. What Was Done Well

- **Clean strategy pattern** — `MarketDataSource` ABC with two interchangeable implementations writing to a shared `PriceCache`; downstream code is fully source-agnostic.
- **Immutable `PriceUpdate`** (`frozen=True, slots=True`) with derived `change`/`change_percent`/`direction` properties and `to_dict()` — correct and efficient.
- **GBM math is correct** — `S·exp((μ − ½σ²)·dt + σ·√dt·Z)`; internal price state keeps full precision and only the emitted value is rounded, so rounding doesn't bleed into the random walk.
- **Correlated moves via Cholesky** of a sector-based correlation matrix; verified positive-definite for the full default watchlist.
- **Defensive, cancellable background loops** in both sources; `stop()` is idempotent and awaits cancellation cleanly.
- **Version-based SSE de-duplication** avoids redundant payloads; cache is seeded on `start()` so the first SSE frame carries data immediately.
- **Thread-safe cache** with a `Lock` — correct given the Massive client runs blocking calls via `asyncio.to_thread`.

---

## 6. Verdict

The subsystem is structurally excellent and the default simulator path is ready. **One blocking issue must be fixed before the Massive integration can be considered functional:**

**Must fix:**
1. ~~**H-1** — Correct the `last_trade` timestamp field (`sip_timestamp`, ns→s) so the real-data path populates the cache.~~ ✅ Fixed 2026-05-25.

**Should fix:**
2. ~~**M-1** — Add a Massive test using a real-shaped snapshot (`TickerSnapshot.from_dict`) to lock the contract and prevent regressions of H-1.~~ ✅ Fixed 2026-05-25.
3. **L-4** — Add a basic ASGI-level SSE test.

**Nice to have:**
4. **L-3** SSE keepalive heartbeat; **L-2** fresh router per factory call; **L-1** lock the `version` read; **N-1** tighten the `massive` version floor.

The simulator path requires no changes. None of these issues block continued development of the rest of the platform, since the default configuration (simulator) is unaffected — but H-1 should be resolved before anyone relies on `MASSIVE_API_KEY`.
