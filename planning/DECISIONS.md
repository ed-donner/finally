# Planning Decisions

This file records concrete decisions made to resolve open questions and contract gaps in `planning/PLAN.md`.

## 2026-04-13

### LLM configuration

- `OPENROUTER_API_KEY` is required only when `LLM_MOCK=false`.
- `LLM_MOCK=true` is a supported no-key development and test mode.
- The backend should fail fast at startup if mock mode is off and the API key is missing.

Reasoning: this preserves the intended low-friction local and CI workflow while keeping production configuration errors obvious.

### Docker persistence

- Local development and test runs use a bind mount from repo `db/` to `/app/db`.
- The plan no longer mixes bind mounts with a named Docker volume.
- `db/finally.db` is intentionally visible on the host for inspection and persistence.

Reasoning: one persistence model is easier for agents to implement consistently, and host-visible SQLite data is useful for a course project.

### Chat API contract

- `/api/chat` returns persisted `user_message` and `assistant_message` objects, including message IDs and timestamps.
- Action results are returned only after execution, under `assistant_message.actions`.
- Partial failures are represented per action with `status` plus `error`, while the overall response remains `200` for valid requests.
- The response also includes post-execution `portfolio` and `watchlist` state so the frontend can reconcile immediately.

Reasoning: this removes frontend/backend ambiguity around inline confirmations, persisted message identity, and partial trade failures.

### Position lifecycle

- Selling a position to zero keeps the row but resets `avg_cost` to `0`.
- A later buy in the same ticker establishes a brand-new cost basis.

Reasoning: unrealized P&L after re-entry should reflect only the new position, not stale historical basis.

### Trade validation

- Tickers are normalized to uppercase and trimmed.
- Unsupported symbols are rejected.
- Quantity must be finite, positive, and no more than 4 decimal places.
- Manual and LLM-originated trades use the exact same validation rules.

Reasoning: shared validation rules prevent drift between frontend behavior, direct API usage, and AI-triggered actions.

### SSE protocol

- `/api/stream/prices` uses named SSE events: `snapshot`, `price`, `watchlist`, and `heartbeat`.
- The server sends an initial `snapshot` immediately on connect.
- Watchlist add/remove operations emit a `watchlist` event on existing connections rather than requiring reconnect.

Reasoning: explicit event types give frontend and backend a stable contract for initial render, live updates, and reconnect behavior.
