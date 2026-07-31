# Review: Changes Since Last Commit

Base commit: `14550e1 Ready for Teams`. Reviewed via `git status`/`git diff` for tracked files, plus
direct reading of all untracked files/directories, cross-checked against `planning/PLAN.md`,
`CLAUDE.md`, `backend/CLAUDE.md`, and the actual implementation in `backend/app/market/`
(`cache.py`, `models.py`, `massive_client.py`, `simulator.py`). All findings below were
independently verified against source, not just asserted.

Files in scope:
- Modified: `.claude/settings.json`, `README.md`
- Untracked: `.claude/agents/` (`change-reviewer.md`, `codex-reviewer.md`, `reviewer.md`),
  `.claude/commands/doc-review.md`, `planning/MARKET_INTERFACE.md`, `planning/MARKET_SIMULATOR.md`,
  `planning/MASSIVE_API.md`, `planning/review.md` (this file)

---

## 1. planning/MARKET_INTERFACE.md, MARKET_SIMULATOR.md, MASSIVE_API.md — CRITICAL

These three new top-level `planning/` files share filenames with, but are near-total rewrites of,
files that already exist in `planning/archive/`. Diffing new vs. archived confirms they are not
duplicates — they describe a materially different design, and the **archived** versions are what
the shipped, tested code in `backend/app/market/` actually implements. `CLAUDE.md` states the
market data component is complete and points readers to `planning/MARKET_DATA_SUMMARY.md` and
`planning/archive/`; it gives no indication a second, competing, top-level copy of these design
docs should exist or that the design is being revisited. Concrete, verified mismatches:

### 1a. PriceCache API mismatch
- `planning/MARKET_INTERFACE.md` (line ~100-102) specifies `self._lock = asyncio.Lock()` and
  `async def set(self, update: PriceUpdate) -> None`, i.e. callers construct `PriceUpdate` objects
  themselves and await an async setter.
- The real `backend/app/market/cache.py` uses `from threading import Lock` (a synchronous lock)
  and a synchronous `def update(self, ticker: str, price: float, timestamp: float | None = None)
  -> PriceUpdate`. The cache itself constructs the `PriceUpdate` and computes `previous_price`.
  There is no `set()` method and nothing here is `async`.
- Code written against the new doc (`await cache.set(PriceUpdate(...))`) would not run against the
  real class at all — wrong method name, wrong signature, wrong sync/async model.

### 1b. PriceUpdate model mismatch
- New doc (`MARKET_INTERFACE.md` lines ~21-31): `timestamp: datetime`, a `Direction(str, Enum)`
  with `UP`/`DOWN`/`FLAT` stored as a field, `@dataclass(frozen=True)` (no `slots`).
- Real `backend/app/market/models.py`: `timestamp: float` (Unix seconds), `direction` is a
  computed `@property` returning a plain `str` ("up"/"down"/"flat"), the dataclass is
  `@dataclass(frozen=True, slots=True)`, and there's a `to_dict()` serialization helper used for
  SSE transmission that the new doc never mentions.

### 1c. MassiveDataSource / Massive API call shape mismatch
- `MARKET_INTERFACE.md` and `MASSIVE_API.md` (line ~140) both sketch
  `client.get_snapshot_all("stocks", tickers=[...])` — market type passed as a raw string — and
  describe parsing `entry.day.close` / `entry.prev_day.close` as the seed for the first poll.
- The real `backend/app/market/massive_client.py` imports `SnapshotMarketType` from
  `massive.rest.models` and calls `get_snapshot_all(market_type=SnapshotMarketType.STOCKS,
  tickers=...)` — an enum, not a string — and only ever reads `snap.last_trade.price` /
  `snap.last_trade.timestamp`; it never touches `day` or `prev_day`. Because the real code funnels
  every poll through the same `cache.update()` used by the simulator, a ticker's first price
  update always has `previous_price == price` ("flat"), not seeded from `prevDay.close` as the new
  doc describes.
- A future agent following the new doc's sketch to "align" or refactor `massive_client.py` would
  break it against the pinned `massive` client — the real code's use of the enum (not a string)
  was presumably arrived at by reading the actual library, and the new doc regresses that.

### 1d. GBMSimulator mismatch — seeding and sector grouping
- `MARKET_SIMULATOR.md` (lines ~13, 112-114) specifies `GBMSimulator(tickers, seed: int | None =
  None)` built on `np.random.default_rng(seed)`, explicitly naming "deterministic enough to test
  (seedable RNG)" as a design goal.
- The real `backend/app/market/simulator.py` has no `seed` parameter — `GBMSimulator.__init__`
  takes only `tickers`, `dt`, `event_probability`, and draws from the **global**
  `np.random.standard_normal` / `random.random()` / `random.uniform()` state rather than an
  injectable `Generator`. This is a genuine behavioral gap versus the doc's own stated goal, not a
  naming difference — tests against the real simulator cannot be seeded the way the doc implies.
- The real `simulator.py` imports `TSLA_CORR` alongside `INTRA_TECH_CORR` / `INTRA_FINANCE_CORR` /
  `CROSS_GROUP_CORR` from `seed_prices.py`, i.e. TSLA is deliberately special-cased with its own
  correlation constant rather than folded into a generic sector map — a distinct design choice the
  new doc's simpler `SECTOR`/`SAME_SECTOR_CORR` scheme doesn't capture.

### Why this matters
Two documents with identical filenames now live in two different `planning/` locations with
contradictory content, and nothing in the repo states which is authoritative. The archived copies
match the real code; the new top-level copies do not. If these are meant to describe a **proposed
future refactor**, that intent needs to be explicit (e.g., "proposed redesign — not yet
implemented, see open questions") so nobody mistakes them for current documentation. If they were
added by accident (e.g., regenerated from a stale prompt without reading the existing
implementation), they should be deleted — stale, wrong specs sitting next to a completed, tested
subsystem are actively harmful, since the natural first move for an agent picking up chat/portfolio
work is to read `planning/*.md`, and these three files would hand it an incorrect contract for
`app/market/`.

**Recommendation:** Either (a) delete these three new files and rely on
`planning/archive/` + `planning/MARKET_DATA_SUMMARY.md`, or (b) if a redesign is genuinely
intended, clearly label them as a proposal, state *why* the change is warranted (none of the
observed differences are motivated in the text as written), and reconcile them with the currently
passing test suite (73 tests per `README.md`/`MARKET_DATA_SUMMARY.md`) before anyone implements
against them.

---

## 2. .claude/settings.json — Medium/High

Adds a `Stop` hook:
```
"command": "if [ -z \"$FINALLY_STOP_HOOK_ACTIVE\" ]; then FINALLY_STOP_HOOK_ACTIVE=1 claude -p 'Use the change-reviewer agent to review all changes since the last commit and write the result to planning/review.md'; fi"
```

- **Re-entrancy guard is correct.** Prefixing `FINALLY_STOP_HOOK_ACTIVE=1` onto the `claude -p`
  invocation scopes the env var to that child process (and any hooks it triggers), so the spawned
  review session's own Stop event sees the guard set and skips re-triggering. No hole found here.
- **High: the hook's prompt and the agent it invokes disagree.** The hook dispatches to "the
  change-reviewer agent" with the instruction "review all changes since the last commit," but
  `.claude/agents/change-reviewer.md`'s body says only: "You review the file planning/Plan.md and
  write your feedback to planning/review.md." This is a real, load-bearing ambiguity — this very
  review run had to decide whether to scope narrowly to `PLAN.md` or broadly to all changes.
  Until `change-reviewer.md`'s body is fixed to match both its own `description` and what the hook
  actually asks it to do, automatic Stop-triggered reviews risk silently narrowing scope back to
  just `planning/PLAN.md`, defeating the hook's purpose (which is specifically to catch drift
  across all changes — including the Sec 1 issue above, which a `PLAN.md`-only review would never
  surface, since `PLAN.md` itself is unchanged in this diff).
- **Medium: runs on every Stop, synchronously, with a full nested `claude -p` invocation, and
  always clobbers `planning/review.md`.** This fires on every turn boundary the harness treats as
  "Stop," not just meaningful checkpoints. Since the target file is always overwritten with no
  append/versioning, a substantive review can be silently replaced by a near-empty/no-op review the
  next time the hook fires with nothing new to review. Consider gating on `git diff --quiet &&
  git status --porcelain` (skip if nothing changed since the last commit) to avoid needless LLM
  calls and noisy overwrites.
- No other keys changed; the `enabledPlugins` block is untouched. JSON is well-formed.

---

## 3. .claude/agents/ (new: change-reviewer.md, codex-reviewer.md, reviewer.md)

### 3a. change-reviewer.md — description/body mismatch (confirmed, causes real ambiguity)
- Frontmatter `description`: "carry out a comprehensive review of all changes since the last
  commit."
- Body: "You review the file planning/Plan.md and write your feedback to planning/review.md."
- As noted in Sec 2, this directly caused scope ambiguity for both the Stop hook and this review
  invocation. Fix by rewriting the body to match the description — review the full diff and
  untracked files, not just `PLAN.md` — since that's clearly the intended behavior given how it's
  invoked from `settings.json`.

### 3b. reviewer.md — near-duplicate with the same mismatch
- `description`: generic "reviews code and provides feedback on improvements, best practices, and
  potential issues."
- `body`: byte-for-byte identical to `change-reviewer.md`'s — "You review the file
  planning/Plan.md and write your feedback to planning/review.md."
- `reviewer.md` and `change-reviewer.md` are currently functionally identical despite different
  names/descriptions, and neither's body matches its own description. Consolidate to one agent, or
  differentiate them explicitly (e.g., `Reviewer` for ad-hoc review of a named doc, `change-reviewer`
  specifically for the "since last commit" workflow the hook uses).
- Naming convention is inconsistent: `change-reviewer` / `codex-reviewer` are lowercase-hyphenated;
  `Reviewer` is TitleCase. Pick one convention.

### 3c. codex-reviewer.md — case-sensitivity bug + undocumented external dependency
- Shells out to: `codex exec "please review the file planning/plan.md and write your feedback to
  planning/review.md"`.
- The actual file is `planning/PLAN.md`, not `planning/plan.md`. This works by accident on macOS's
  default case-insensitive filesystem but will fail to find the file on any case-sensitive
  filesystem — Linux, most CI runners, and the project's own Docker image (Python 3.12 slim on
  Linux, per `PLAN.md` §11). Fix the casing.
- This agent depends on an external `codex` CLI not mentioned anywhere in `CLAUDE.md` or
  `planning/PLAN.md` as part of the toolchain (`uv`, Node/npm, and Docker are the documented
  stack). If `codex` isn't installed/authenticated, this agent fails with a raw shell error rather
  than a clear message. At minimum document the dependency; ideally add a pre-check with a clear
  failure message.

### 3d. Overlapping write targets, no coordination
- `change-reviewer.md`, `reviewer.md`, and `codex-reviewer.md` (via `codex`) all unconditionally
  overwrite `planning/review.md` — no append, no timestamp, no namespacing. The previous run's
  feedback is silently lost every time any of the three fires. With the new Stop hook auto-firing
  one of them on every Stop event, this is no longer theoretical.
- `.claude/commands/doc-review.md` uses a *different* convention entirely — it appends findings
  into a new section **within the reviewed doc itself**, not a separate file. Two incompatible
  "where does feedback go" conventions now exist side by side in the same change set; worth
  standardizing on one.

### 3e. No tool-access restriction on any of the three new agents
- None declare a `Tools:` frontmatter field, so all default to full tool access (confirmed via the
  environment's agent listing, which shows "Tools: All tools" for all three). Given their stated
  job — read a doc (or diff), write feedback to another doc — scoping to read-only plus a narrow
  write allowance (no arbitrary Bash/network) would reduce blast radius, particularly for
  `codex-reviewer.md`, which already shells out to an external binary.

---

## 4. .claude/commands/doc-review.md

- Typo: "add questions, clarifications, or feedback **toa** new section at the end" — missing
  space, should read "to a new section."
- See Sec 3d above: this command's "append into the same file" convention conflicts with the
  agents' "overwrite a separate `planning/review.md`" convention. Both patterns exist in this
  change set for functionally the same task ("review a planning doc").

---

## 5. README.md — no issues found

The diff accurately reflects reality: it demotes "Quick Start" (the Docker one-liner) to a
"planned" state, adds an honest "Status" section calling out that only the market data backend
(`backend/app/market/`) is built, links to `planning/MARKET_DATA_SUMMARY.md`, and replaces the
Docker quick-start with the actual runnable command (`cd backend && uv sync && uv run
market_data_demo.py`), which matches `backend/CLAUDE.md`'s documented demo command and the
`backend/market_data_demo.py` file present in the repo. The trimmed directory-structure block
(dropping `frontend/`, `test/`, `db/`, `scripts/`, none of which exist yet) is also accurate. Low
risk, well-scoped, nothing to flag.

Minor nit (not worth a severity tier): the "Not yet started" bullet list mentions "Database schema,
portfolio/trade endpoints, watchlist endpoints" but doesn't explicitly call out the `chat_messages`
table / chat persistence from `PLAN.md` §7 — a very small gap, purely cosmetic.

---

## Summary by Severity

**Critical**
- Sec 1: `planning/MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md` describe a design
  that diverges from the actual, tested `backend/app/market/` implementation in at least four
  concrete, verified ways (async vs. sync `PriceCache` API, `PriceUpdate` shape/types, Massive
  client call signature and fields read, `GBMSimulator` seeding and TSLA sector handling) — and
  they duplicate/shadow filenames that already exist, correctly, in `planning/archive/`, with no
  authoritativeness marker anywhere in the repo. This is the highest-impact issue: left as-is, it
  will actively mislead whoever next extends the market data layer or builds downstream code
  against it (portfolio valuation, SSE consumers, chat trade execution).

**High**
- Sec 2 / 3a: `change-reviewer.md`'s body ("review planning/Plan.md") doesn't match either its own
  `description` or the prompt the new Stop hook actually sends it ("review all changes since the
  last commit"). This is a live bug — this very review run had to resolve that exact ambiguity —
  and if resolved the wrong way by a future automated run, it would have missed the Sec 1 issue
  entirely, since `PLAN.md` itself is unchanged in this diff.

**Medium**
- Sec 2: Stop hook fires unconditionally on every Stop event and always clobbers
  `planning/review.md` with no versioning; consider a `git diff --quiet` guard to skip no-op runs.
- Sec 3b-3e: `reviewer.md` duplicates `change-reviewer.md` with the same description/body mismatch;
  `codex-reviewer.md` has a case-sensitivity bug (`plan.md` vs. `PLAN.md`) that will break on
  Linux/CI and an undocumented `codex` CLI dependency; three agents plus one slash command
  implement two incompatible "where does feedback go" conventions; none of the new agents restrict
  tool access.

**Low**
- Sec 4: Typo ("toa" → "to a") in `.claude/commands/doc-review.md`.
- Sec 5: `README.md`'s "Not yet started" list omits explicit mention of chat message persistence
  (very minor, cosmetic).

**No issues**
- Sec 5: `README.md` changes are accurate, honest about project status, and well-scoped.
