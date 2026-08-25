# PLAN.md Diff Review — DeepSeek (Codewhale)

**Scope**: all changes since last commit (`git diff HEAD`), i.e. `planning/PLAN.md` (modified) plus the untracked files `planning/REVIEW.md`, `planning/review-claude.md`, `.claude/agents/*`, `.claude/commands/*`, `.codewhale/*`. Findings were cross-checked against the already-built `backend/app/market/*` code.

## Verdict

The `PLAN.md` diff is a well-targeted follow-up to a prior doc-review round. It resolves real ambiguities — tracked tickers vs. watchlist, sparkline seeding, the watchlist cap, chat-history bound, the `watchlist_changes.action` enum, the `backend/db` vs `db` naming collision — and the §13 changelog accurately describes *what text changed*. The substantive problem is a **spec-vs-implementation mismatch**: the diff writes several resolutions in past tense ("Added…", "All were resolved") inside a plan whose sibling docs already declare the market-data component "complete", but the corresponding code does not contain the feature. A downstream agent can reasonably read these as shipped, and they are not.

## Findings (priority order)

### 1. HIGH — History buffer + `/api/market/history/{ticker}` documented as resolved, but not implemented

New text (§6, §8, §13):
> `PriceCache`, which holds, per ticker: … and **a bounded rolling history buffer (most recent 500 points)**
> | No historical price data | Added an in-memory rolling history buffer (500 points/ticker) in `PriceCache` + `GET /api/market/history/{ticker}` … |

Verified against `backend/app/market/cache.py`: `PriceCache` stores only `self._prices: dict[str, PriceUpdate]` — the latest update per ticker, with no history array, no 500-point bound, no pruning. A grep across `backend/app/market/` finds **no `/history` route**; the only route is `@router.get("/prices")` under `/api/stream`. `planning/MARKET_DATA_SUMMARY.md` calls the market-data subsystem "Complete, tested, reviewed, all issues resolved," so nothing signals to a frontend agent that `GET /api/market/history/{ticker}` and the buffer are still outstanding backend work.

**Fix**: reword §13 so it is unambiguous that this is a *plan-level* resolution, not a shipped one — e.g. "Resolved in the spec; **not yet implemented** — `PriceCache` currently holds only the latest price per ticker, so the 500-point buffer and `/api/market/history/{ticker}` are outstanding backend work." Add the same one-line caveat to `MARKET_DATA_SUMMARY.md`.

### 2. MEDIUM — "4 decimal places" fractional-quantity rule is stated only for the UI, not as a server invariant

Fractional quantities are introduced by this diff. The 4-dp rule appears solely in §10 (the "Trade bar" bullet, UI-only), and §9 explicitly points an LLM-issued trade's rule back to §10 ("see §10"). But the LLM and any direct API caller never pass through the trade-bar UI, and `POST /api/portfolio/trade` (§8) still specifies no validation rule. It is therefore undefined whether 4-dp rounding is (a) a UI input mask only, or (b) a server-side invariant on every trade regardless of source (manual, chat/LLM, test). This is a real fork for the backend implementer.

**Fix**: state 4-dp rounding (plus "finite, positive quantity") as a server-side validation rule in §7/§8, applied uniformly to manual and LLM-issued trades, and have §10 reference it rather than be the sole source.

### 3. MEDIUM — SSE wire description remains wrong in a section the diff edited

The diff rewrote the surrounding bullets ("Shared Price Cache & History", "Tracked Tickers") but left this sentence effectively intact:
> Each SSE event contains ticker, price, previous price, timestamp, and change direction

This reads as one event per ticker. The implemented `backend/app/market/stream.py` instead yields one **batched** JSON object per send (`data: {"AAPL": {...}, "GOOGL": {...}}`), gated on a version counter, with no `event:` name and no heartbeat (only a `retry: 1000` directive). Since the diff was already editing this exact section, correcting the per-event framing to match the shipped wire format was in-scope; leaving it means the frontend contract is still wrong in a section this diff specifically touched.

**Fix**: update the SSE bullet to describe the batched-payload shape, version-gated sends, and absence of heartbeat — or flag it explicitly as a known frontend-contract gap.

### 4. MEDIUM — "daily change %" is required by the UI but not producible from the data model

§10 requires "daily change %" in the watchlist panel and positions table, but `PriceUpdate`/`PriceCache` only stores `price` and `previous_price` (tick-over-tick). `PriceUpdate.change_percent` computes change from the *previous tick*, not from a session-open or previous-close baseline. Labeling tick movement "daily change" would be misleading, and the plan never resolves the source. This predates the diff but the diff touched the price-cache and UI bullets without closing it.

**Fix**: define daily-change semantics explicitly (previous close / session open / tick-over-tick), or drop the "daily" qualifier and relabel as "change %".

### 5. LOW — §13 "resolved" framing invites over-reading more broadly

The closing line "All were resolved and folded into the sections above" is accurate for a *doc* review but easy to misread project-wide. Two rows (Tracked Tickers, Massive rate-limit) are already true of the code — verified: `massive_client.py` batches all tracked tickers into one `get_snapshot_all(tickers=self._tickers)` call — but the history-buffer row is not, and a reader cannot tell the difference without cross-checking source.

**Fix**: split the table into "spec-level" vs "implemented" resolutions, or add a one-line caveat above it ("plan-text resolutions; check `backend/app/market/` for implementation status").

### 6. LOW — "full original notes in git history" is inaccurate

§13 says the review's "full original notes" are "in git history", but the review artifacts (`planning/REVIEW.md`, `planning/review-claude.md`) are untracked working-tree files, not committed history. Minor, but it overstates the audit trail a reader can actually `git log` for.

### 7. LOW — Adjacent Massive polling interval left inconsistent while the section was open

The diff added a new Massive bullet about batched polling but left the neighboring bullet "Paid tiers: poll every 2-15 seconds depending on tier", whereas `massive_client.py`'s own docstring says paid tiers poll "every 2-5s". Not introduced by this diff, but a low-cost reconciliation since the bullet block was already being edited.

### 8. LOW — Untracked review/agent scaffolding has cosmetic defects

- `.claude/agents/codewhale-reviwer.md`: filename/`name` typo ("reviwer"), "write you feedback", and a self-contradictory instruction set — it tells the agent to run `codewhale execute "…"` and then says "DO NOT RUN THE REVIEW YOURSELF."
- `.claude/commands/doc-review.md`: `$ARGUEENTS` (typo; shell variables are case-sensitive, so this would expand empty).
- `planning/review-claude.md` references `planning/DSeekREVIEW.md`, which does not exist in the working tree (only `REVIEW.md` and `review-claude.md` are present) — a dangling cross-reference.

## Consistency notes (verified, no problem found)

- The 20-ticker watchlist cap is stated consistently across §2, §6, §8, and does not conflict with the 10-ticker default seed list in §7.
- "Tracked tickers = watchlist ∪ positions.ticker" is introduced once in §6 and referenced (not restated) from §8 — no duplication problem.
- The `chat_messages.actions` denormalization note (§7) explicitly subordinates itself to `trades`/`watchlist`; no contradiction.
- `backend/db/` → `backend/database/` rename (§4): the repo currently has neither directory (`backend/app/market/` holds market data), so this is a clean, uncontested fix for the naming collision, not a rollback of completed work.
- §13's "7 questions plus feedback on naming/duplication/repetition" count matches the 10 table rows (7 substantive + 3 naming/duplication/repetition).

## Note on sibling reviews

`planning/REVIEW.md` and `planning/review-claude.md` are both reviews of the *current post-diff* `PLAN.md`, not of a prior version. Their highest-severity items (spec-vs-code mismatch for the history buffer, SSE wire-format gap) were re-verified here against source and corroborate findings 1 and 3. Their remaining items (full request/response schemas, trade atomicity, numeric/ticker validation, static-export routing, mock-chat contract, snapshot retention) are real but pre-existing gaps in `PLAN.md`, not introduced or worsened by this diff — appropriately out of scope for a diff-focused review and worth folding into a future revision.
