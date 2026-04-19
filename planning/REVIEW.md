# Review of `planning/PLAN.md`

## Overall Feedback

This is a strong project plan. It has a clear product vision, a coherent architecture, and a good explanation for why the main technology choices were made. That is already better than many early project plans.

The main issue is not that the plan is weak. The main issue is that it is ambitious. For a junior developer, the current document explains **what** the product should be, but it does not always explain **in what order to build it**, **what can be simplified first**, and **what exact contracts different parts of the system should follow**.

In short: the plan is exciting and well-scoped at the product level, but it still needs more implementation-level guidance.

## What Is Working Well

- The vision is easy to understand and gives the project a strong identity.
- The architecture choices are mostly sensible for a teaching project: FastAPI, SQLite, SSE, one container, one port.
- The plan correctly avoids unnecessary complexity in some places, especially by using market orders only and defaulting to a simulator.
- The testing section is a good sign. It shows that quality was considered early.
- The built-in review notes in Section 13 are useful and catch several real gaps.

## Main Suggestions for Improvement

### 1. Define a clear MVP before the “full demo” version

Right now the plan mixes core requirements and “wow factor” features together. That makes implementation riskier.

For a junior developer, I would strongly recommend splitting the work into:

- **MVP**: watchlist, streaming prices, manual buy/sell, portfolio summary, simple chat request/response
- **Phase 2**: treemap, richer charts, watchlist management through AI, better animations
- **Phase 3 / stretch**: cloud deployment, more polished mock behavior, advanced charting improvements

This matters because a good software plan should make it obvious what can be cut if time runs short.

### 2. Add a milestone-based implementation order

The document describes the final product well, but not the build sequence.

That is important for a junior developer because they need a safe path like:

1. Create backend health check and static frontend serving
2. Set up SQLite schema and seed data
3. Implement simulator and in-memory price cache
4. Add SSE stream and verify prices update in browser
5. Implement manual trade API and portfolio calculations
6. Build basic frontend watchlist and portfolio table
7. Add chat endpoint with `LLM_MOCK=true`
8. Integrate real LLM only after mock mode works
9. Add charts, treemap, and UI polish last

Without this order, it is easy to start with the flashy parts and get blocked by the fundamentals.

### 3. Define API response shapes, not just endpoint names

This is one of the biggest practical gaps.

A frontend developer and a backend developer can both follow the current plan and still build incompatible implementations. The plan should include example JSON responses for at least:

- `GET /api/portfolio`
- `GET /api/watchlist`
- `GET /api/portfolio/history`
- `POST /api/portfolio/trade`
- `POST /api/chat`

For a junior developer, explicit contracts reduce confusion and rework.

### 4. Resolve open questions inside the plan, not only in the review notes

Section 13 identifies good problems, but they are still left as open issues. A stronger version of the plan would move the decisions back into the earlier sections.

Examples:

- What exactly is included in SSE: watchlist only, or watchlist + held positions?
- How many chat messages are included in LLM history?
- What does “daily change %” mean in simulator mode?
- What is the exact mock chat response used in tests?

A plan is more helpful when it contains decisions, not just observations.

### 5. Simplify the LLM scope and describe failure behavior more carefully

The AI assistant is one of the riskiest parts of the project because it combines prompting, structured outputs, validation, side effects, and UI updates.

For a junior developer, I would suggest narrowing the first version:

- Start with chat that returns a message only
- Then add structured watchlist actions
- Then add structured trade actions
- Only after that allow auto-execution

If auto-execution stays in scope from day one, the plan should clearly define:

- what happens when the model returns invalid JSON
- what happens when only some requested trades succeed
- whether the backend edits the assistant message after validation fails
- what response shape the frontend receives for partial success

### 6. Add explicit trade and portfolio rules

The plan describes the database schema, but some business rules are still implicit.

For example, the plan should answer these directly:

- Are fractional shares allowed everywhere in the UI and API?
- How many decimal places are supported?
- How is average cost recalculated after multiple buys?
- What happens when selling the entire position?
- Are negative or zero quantities rejected with `400 Bad Request`?
- Are unknown tickers allowed if they are not already in the watchlist?

Junior developers often struggle less with coding than with unclear business rules.

### 7. Add error states and empty states to the UX section

The user experience section focuses on the ideal path, which is good, but implementation also needs non-happy-path behavior.

The plan should describe:

- what the UI shows when SSE disconnects
- what happens if the market data provider is unavailable
- what happens if the chat request fails
- what the portfolio area shows when there are no positions
- what the chart shows before enough data exists

These states are easy to forget and often create rough demos.

### 8. Reduce accidental complexity in persistence

The snapshot idea is useful, but the retention policy should be defined now. Otherwise the table can grow forever.

A simple rule would be enough:

- keep snapshots for the last 24 hours, or
- keep the latest N rows, or
- store every 30 seconds for 30 minutes, then downsample older data

The important thing is to choose a rule before implementation.

### 9. Make the testing plan more priority-driven

The testing section is good, but still broad. A junior developer benefits from knowing which tests matter most.

I would recommend stating a minimum required test set:

- one backend test for trade validation
- one backend test for portfolio calculation
- one backend test for simulator output format
- one E2E test for first launch
- one E2E test for manual buy flow
- one E2E test for mocked chat flow

This gives a realistic testing floor before adding more coverage.

### 10. Fix a few inconsistencies before anyone starts coding

These are small, but they can waste time:

- The model identifier in the LLM section should be verified before implementation.
- The Docker section should explicitly describe where the built frontend files live in the final image.
- The Docker steps should clearly mention `uv.lock`.
- The plan should state whether sparkline data is intentionally temporary and reset on page refresh.

## Suggested Additions

If this plan is going to guide implementation, I would add three short sections:

### A. Non-Goals

This helps prevent scope creep. For example:

- No real-money trading
- No authentication or multi-user support in v1
- No order book or limit orders
- No portfolio import/export

### B. Definition of Done

This helps a junior developer know when the project is actually complete. For example:

- App starts with one documented command
- Default watchlist loads
- Prices update without manual refresh
- User can buy and sell successfully
- Portfolio values update correctly
- Chat works in mock mode
- E2E tests pass

### C. Implementation Milestones

A short milestone table would make this much easier to execute and review.

## Final Recommendation

I would not rewrite the whole plan. The foundation is solid. I would do a focused revision with these goals:

1. Separate MVP from stretch features
2. Add milestone order
3. Add response schemas and business rules
4. Resolve Section 13 ambiguities directly in the main sections
5. Reduce LLM ambiguity and define failure handling

If those changes are made, this will become much easier for a junior developer to implement successfully and much less likely to produce mismatched frontend/backend work.
