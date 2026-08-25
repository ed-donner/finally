# PLAN.md Review

## Review — 2026-08-24

The plan has a clear product boundary and resolves the earlier watchlist, price-history, and chat-context gaps. The remaining items are chiefly contracts that need to be explicit before frontend and backend work proceeds independently.

### Questions and clarifications

1. **Define complete request and response schemas.** The endpoint table names the routes but does not specify response payloads, required-field validation, success status codes, or an error envelope. In particular, document the shapes for portfolio positions/P&L, watchlist prices and daily change, history points, trade results, chat actions and per-action failures, and SSE events. This is the most important shared contract for parallel implementation.
2. **Specify trade atomicity and price semantics.** State that a trade validates and updates cash, position, trade history, and immediate portfolio snapshot in one SQLite transaction. Also define the authoritative fill price: the latest `PriceCache` price at the transaction's start, including the behavior when a ticker has no current price or is being added to tracking.
3. **Define numeric and ticker validation centrally.** Document canonical ticker normalization (for example, uppercase and trimming), supported symbols, maximum ticker length, finite positive quantity requirements, four-decimal rounding rule, currency rounding, and the treatment of a position whose remaining quantity is near zero. These rules must be shared by manual trades, chat actions, and watchlist routes.
4. **Resolve market-data startup and failure behavior.** Specify how much history the simulator pre-populates before the first client request; whether the API returns an empty/partial history while a newly tracked ticker is warming up; and how Massive failures, invalid symbols, stale prices, and rate-limit responses appear in the API/UI. A stale-price timestamp/status is needed so the user is not shown a live-looking price after a failed poll.
5. **Define SSE wire and connection behavior.** State the event name and JSON schema, heartbeat interval, retry interval, ordering/deduplication expectations, and whether each event is a single ticker update or a batch. The connection dot also needs exact transitions for initial connection, a healthy but stale stream, errors, and reconnection.
6. **Clarify daily-change source.** The UI requires daily change %, but the cache only defines latest and previous tick prices. Decide whether this is tick-over-tick change, a session-open/previous-close change, or unavailable for the simulator. Labeling tick movement as daily change would be misleading.
7. **Resolve the database-volume wording.** Section 11 calls `finally-data` a named Docker volume, while the following paragraph says the project-root `db/` directory maps to `/app/db`; the shown `docker run` command uses the named volume. Choose one default and update the Dockerfile, Compose file, and scripts consistently. If persistence across application updates is required, specify the documented reset procedure.
8. **Define initialization and snapshot retention.** Clarify whether startup initialization happens only in the FastAPI lifespan handler or may happen on requests, how concurrent requests avoid duplicate seeding, whether an initial portfolio snapshot is inserted, and how/when portfolio snapshots are pruned. The 30-second recorder should also define its behavior when price data is unavailable.
9. **Make mock-chat behavior contractual.** List deterministic mock inputs and resulting actions, including at least one buy, sell, invalid action, and no-action response. E2E scenarios can then assert stable outcomes rather than depend on implementation-specific keyword matching.
10. **Document static-export routing.** Define how FastAPI serves static assets and falls back to the exported SPA entry point without intercepting `/api/*`. This avoids deep-link 404s and route-order ambiguity in the single-container deployment.

### Opportunities to simplify

- Replace repeated prose references to tracked tickers with a single invariant: `tracked_tickers = watchlist ∪ open_position_tickers`; link to it from the market-data, trade, and watchlist sections.
- Consolidate the endpoint table and frontend data requirements into one compact API-contract section with schemas and error rules. It removes duplication while making the interfaces implementable.
- Treat the future multi-user fields as schema preparation only. Because every request currently uses `"default"`, avoid claiming that the in-memory `PriceCache` already supports multi-user behavior; it is globally keyed and market data is shared. A short future-auth note is clearer.
- Choose one charting library rather than leaving two alternatives. This reduces dependency, rendering, and test uncertainty for the frontend implementation.
