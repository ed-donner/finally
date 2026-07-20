# Review — Changes Since Last Commit

Review of uncommitted work on branch `start` (base commit `6b568a9`): three new planning documents, `MASSIVE_API.md`, `MARKET_INTERFACE.md`, and `MARKET_SIMULATOR.md`, researching and designing the market-data layer described in `PLAN.md` §6.

## Summary of Changes

- **`planning/MASSIVE_API.md`** (new): Research notes on the Massive (formerly Polygon.io) REST API — auth, rate limits, and the specific endpoints needed for multi-ticker real-time quotes and end-of-day prices.
- **`planning/MARKET_INTERFACE.md`** (new): Design for the unified `MarketDataSource` abstract interface selected by `MASSIVE_API_KEY` presence, implemented by both the Massive-backed source and the simulator.
- **`planning/MARKET_SIMULATOR.md`** (new): GBM-based price simulator design — correlated sector moves, random event shocks, seed prices, tick loop.

## Findings

- **Unverified external claims (`MASSIVE_API.md`)**: The endpoint shapes, auth header, and rate limits were pulled from Massive's public docs and a few third-party summaries via web search/fetch, not confirmed against a real API key/response. Before backend implementation begins, a quick live smoke test against the actual API (with a real `MASSIVE_API_KEY`) should confirm field names (`lastTrade.p`, `prevDay.c`, `updated` in nanoseconds) and the auth header format, since some of the fetched doc pages returned incomplete content and had to be corroborated from the Python client's debug output rather than an explicit curl example.
- **Interface not yet wired to PLAN.md's open questions**: `MARKET_INTERFACE.md` §7 proposes an answer to the "delete a watched ticker with an open position" question raised previously (keep it watched via position-derived `watch()` calls), and `MASSIVE_API.md` §5 proposes an answer to the "unrecognized ticker" question (treat an empty snapshot result as invalid rather than a separate validation call). Neither of these is yet reflected back into `PLAN.md` itself — worth folding in as explicit rule updates so `PLAN.md` stays the single source of truth rather than requiring readers to cross-reference three documents.
- **No rounding/precision decision made**: `MARKET_SIMULATOR.md` rounds simulated prices to 4 decimals ad hoc; `MASSIVE_API.md`/`MARKET_INTERFACE.md` don't specify a rounding convention for cash/price values at all. This was flagged as an open question against `PLAN.md` previously and remains unresolved by this round of docs — should be decided before the backend's trade-execution math is implemented, since simulator and Massive-backed prices need to agree on precision for `PriceTick.price` to be a true drop-in-compatible contract.
- **Poll-interval fallback behavior newly specified but not yet approved**: `MASSIVE_API.md` §4 states that on request failure/429 the poller should skip the tick and keep serving cached prices rather than falling back to the simulator. This is a reasonable default but is a product decision (not purely technical) and should get explicit sign-off, since PLAN.md itself left it as an open question rather than a decided behavior.

## Verdict

No code was changed — this is planning/documentation only, so there's no runtime risk yet. The main follow-up is reconciling the new design decisions back into `PLAN.md` (or leaving them here as an addendum) so there's one authoritative document, and validating the Massive API details against a live key before backend work starts.
