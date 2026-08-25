# PLAN.md Diff Review — Claude

**Scope**: `git diff planning/PLAN.md` (93 insertions / 52 deletions), reviewed against the rest of the document, `planning/MARKET_DATA_SUMMARY.md`, the already-built `backend/app/market/*` code, and the two sibling untracked reviews (`planning/REVIEW.md`, `planning/DSeekREVIEW.md`), which both turn out to already be reviews of this same post-diff text.

## Verdict

The diff is a coherent, well-targeted response to a prior review round — it resolves seven real ambiguities (tracked-tickers vs. watchlist, sparkline seeding, fractional quantities, watchlist cap, chat-history bound, `watchlist_changes.action` enum, the `backend/db`/`db` naming collision) and the §13 changelog's description of *what text changed* is accurate. The main problem is not the prose itself but a **documentation/implementation mismatch that the diff creates**: it writes several of these resolutions in the past tense ("Added...", "resolved") inside a project where a sibling doc and `CLAUDE.md` already declare the market-data component "complete" — but the corresponding code does not contain the feature. A reader (especially a downstream frontend agent) can reasonably conclude these are shipped, and they are not.

---

## Findings (priority order)

### 1. HIGH — §13 documents the history-buffer feature as resolved; it does not exist in the already-"completed" market-data code

Diff text (new):
> A single background task (simulator or Massive poller) writes to an in-memory `PriceCache`, which holds, per ticker: the latest price, previous price, timestamp, and **a bounded rolling history buffer (most recent 500 points)**
> ...
> | No historical price data | Added an in-memory rolling history buffer (500 points/ticker) in `PriceCache` + `GET /api/market/history/{ticker}` to seed charts on load | §6, §8, §10 |

I checked `backend/app/market/cache.py` directly: `PriceCache` stores only `self._prices: dict[str, PriceUpdate]` — the latest update per ticker, nothing else. There is no history array, no 500-point buffer, no pruning logic, and no `history` route anywhere under `backend/app/market/` (the only stream route is `/api/stream/prices`). `planning/MARKET_DATA_SUMMARY.md` states the market-data subsystem is "Complete, tested, reviewed, all issues resolved," and root `CLAUDE.md` says the market-data component "has been completed." Put together, a downstream agent reading §13 plus those two documents has no signal that `GET /api/market/history/{ticker}` is still unbuilt — the changelog phrasing ("Added...", "All were resolved") reads as a statement about the codebase, not just the spec text.

**Fix**: Reword the §13 entry to make clear this is a *specification* resolution, not a shipped one — e.g. "Resolved at the plan level; **not yet implemented** — `PriceCache` currently holds only the latest price per ticker, so `/api/market/history/{ticker}` and the 500-point buffer are outstanding backend work." Also worth a one-line addendum to `MARKET_DATA_SUMMARY.md` noting the buffer is a planned extension to the "complete" subsystem, so the two docs don't contradict each other. (This mirrors `planning/DSeekREVIEW.md` finding A1, which verified the same gap independently against the code.)

### 2. MEDIUM — New "4 decimal places" rounding rule is stated only for the frontend trade bar, not as a server-side validation rule for API/LLM-issued trades

Diff text (new, §2 / §9 / §10):
> **Buy and sell shares** — market orders only, **fractional quantities supported**...
> `trades` (optional): Array of trades to auto-execute. **`quantity` may be fractional (see §10)**. Each trade goes through the same validation as manual trades...
> **Trade bar** — ... quantity field (**accepts decimal input, rounded to 4 decimal places**), buy button, sell button.

Fractional quantities are a brand-new concept introduced by this diff (the schema's `quantity REAL` was already fractional-capable, but the plan previously implied whole-share trading only). The 4-decimal-place rounding rule is stated exclusively in §10, which is UI-only ("Trade bar"). §9 explicitly points an LLM-issued trade's fractional-quantity rule back to §10 ("see §10"), but the LLM never goes through the trade-bar UI — it calls the trade path directly. §8's `POST /api/portfolio/trade` description (`{ticker, quantity, side}`) still has no validation rule at all. This leaves it undefined whether 4-decimal rounding is:
- a UI input mask only (so the API accepts arbitrary precision), or
- a server-side invariant enforced on every trade regardless of source (manual API call, chat/LLM trade).

Given the LLM can supply quantities like `3.33333`, and the API can be called directly by a test or future client, this is a real fork point for the backend implementer.

**Fix**: State the 4-decimal rounding (and "must be finite, positive" — already flagged in REVIEW.md #3) as a server-side validation rule in §7 or §8, applying uniformly to manual trades and LLM-issued trades, and have §10's trade-bar bullet reference it rather than being the sole source of the rule.

### 3. MEDIUM — Diff touches the Massive/SSE sections but leaves the adjacent per-event SSE description effectively unspecified/incorrect

The diff rewrites "Shared Price Cache" → "Shared Price Cache & History" and rewords the SSE bullet to route through the new "Tracked Tickers" section, but it leaves this sentence essentially untouched in spirit:
> Each SSE event contains ticker, price, previous price, timestamp, and change direction

This reads as one event per ticker. The already-built `backend/app/market/stream.py` instead sends one **batched** JSON object per send (`{"AAPL": {...}, "GOOGL": {...}}`), gated on a version counter, with no `event:` name and no heartbeat. Since the diff was already touching this exact section (adding "Tracked Tickers" and rewiring the history-preload flow through it), it was a natural place to also correct the per-event framing to match the implemented wire format — leaving it as-is means the frontend contract is still wrong in a section this diff specifically edited. (Independently confirmed against code in `planning/DSeekREVIEW.md` A3.)

**Fix**: While touching §6 again for finding #1, also update the SSE bullet to describe the batched-payload shape, version-gated sends, and absence of heartbeat — or explicitly flag it as a known frontend-contract gap if you don't want to change behavior yet.

### 4. LOW — §13's framing invites over-reading "resolved" as "implemented" more broadly

Beyond the history-buffer row specifically (#1), the section's closing sentence — "All were resolved and folded into the sections above" — is accurate for a documentation review (the prior round's *questions* were resolved by editing text) but is easy to misread project-wide, especially next to `MARKET_DATA_SUMMARY.md`'s "all issues resolved" for the code. Two rows in the table (Tracked Tickers, Massive rate-limit) *are* already true of the shipped code — verified: `massive_client.py` does batch all tracked tickers into one `_fetch_snapshots` call — but the history-buffer row is not, and a reader has no way to tell the difference without cross-checking source.

**Fix**: Either split the table into "spec-level" vs. "implemented" resolutions, or add a one-line caveat above the table: "These are plan-text resolutions; check `backend/app/market/` for current implementation status of any given item."

### 5. LOW — Minor, adjacent inconsistency not fixed while the section was open

The diff added a new Massive bullet about batched polling but left the pre-existing, unrelated bullet in the same section reading "Paid tiers: poll every 2-15 seconds depending on tier," while `massive_client.py`'s own docstring says paid tiers poll "every 2-5s." Not introduced by this diff, but since the diff was already editing this exact bullet block, it was a low-cost opportunity to reconcile. (Also flagged in `planning/DSeekREVIEW.md` B8.)

### 6. Informational — no consistency problems found in the core additions

Checked and consistent:
- The 20-ticker watchlist cap is stated uniformly across §2, §6, and §8, and doesn't conflict with the 10-ticker default seed list in §7.
- "Tracked tickers = watchlist ∪ positions.ticker" is introduced once in §6 and referenced (not restated) from §8's SSE description — no duplication problem.
- The `chat_messages.actions` denormalization note (§7) doesn't contradict the trades/watchlist tables elsewhere; it explicitly subordinates itself to them.
- Directory rename `backend/db/` → `backend/database/` (§4): checked the actual repo — no `backend/db` or `backend/database` directory has been created yet (market data lives entirely in `backend/app/market/`), so this rename isn't undoing already-completed work; it's a clean, uncontested fix for the naming collision the changelog claims to resolve.
- §13's "7 questions plus feedback on naming/duplication/repetition" count checks out against the 10 table rows (7 substantive questions + 3 naming/duplication/repetition items), so the summary sentence isn't miscounted.

---

## Note on `planning/REVIEW.md` and `planning/DSeekREVIEW.md`

Both are reviews of the **current (post-diff) PLAN.md**, not of a prior version — `DSeekREVIEW.md` says so explicitly ("includes the §13 doc-review changelog"), and `REVIEW.md`'s framing ("resolves the earlier watchlist, price-history, and chat-context gaps... remaining items are chiefly contracts") is consistent with that too. So they aren't independent context to compare against the diff — they're downstream evaluations of it, and this review's finding #1 corroborates (and was cross-checked against source for) `DSeekREVIEW.md`'s A1/A3, the two highest-severity items there. Their broader findings (full request/response schemas, trade atomicity, numeric validation, static-export routing fallback, etc.) are real but are pre-existing gaps in PLAN.md, not things this specific diff introduced or worsened — they're appropriately out of scope for a diff-focused review and worth folding into a future revision.
