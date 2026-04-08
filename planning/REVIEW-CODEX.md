# Review of `planning/PLAN.md`

## Findings

### High: Portfolio snapshot policy cannot support the promised live P&L chart

`PLAN.md` promises "a P&L chart tracking total portfolio value over time" in the product experience, and the frontend depends on `GET /api/portfolio/history` for that chart. But the database section says `portfolio_snapshots` are recorded only "immediately after each trade execution and on backend startup." That means the chart will not move with market prices unless the user trades, which materially breaks the trading-terminal feel and makes the chart misleading during normal hold periods.

References:
- [PLAN.md:28](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L28)
- [PLAN.md:226](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L226)
- [PLAN.md:254](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L254)
- [PLAN.md:363](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L363)

Recommendation: define a background snapshot cadence now, for example every N seconds while positions exist, plus on trade execution and startup.

### High: Monetary values and share quantities are stored as SQLite `REAL`

The schema stores `cash_balance`, `quantity`, `avg_cost`, `price`, and `total_value` as `REAL`. For a trading app, repeated buy/sell operations and portfolio recomputations will accumulate floating-point drift. That will leak into balances, P&L, and tests, especially once fractional shares are supported.

References:
- [PLAN.md:203](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L203)
- [PLAN.md:214](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L214)
- [PLAN.md:215](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L215)
- [PLAN.md:222](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L222)
- [PLAN.md:223](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L223)
- [PLAN.md:228](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L228)

Recommendation: specify fixed-precision handling now. Integer cents plus scaled share quantities, or Python `Decimal` with explicit rounding rules, would both be safer than `REAL`.

### High: Trade execution is not specified as atomic

A single trade updates cash, positions, trade history, and portfolio snapshots. The plan does not require these writes to happen inside one SQLite transaction. Without that requirement, any exception or concurrent request can leave the portfolio in a partially updated state.

References:
- [PLAN.md:201](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L201)
- [PLAN.md:211](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L211)
- [PLAN.md:218](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L218)
- [PLAN.md:226](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L226)
- [PLAN.md:253](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L253)
- [PLAN.md:294](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L294)

Recommendation: state explicitly that manual trades and chat-triggered trades execute inside a single DB transaction, and define the write order and rollback behavior.

### Medium: SSE semantics conflict with the optional real-data polling model

The plan says the server pushes price updates at about 500ms, but the Massive path may only refresh underlying data every 15 seconds on the free tier. As written, this implies either repeated duplicate "updates" every 500ms or two materially different runtime behaviors depending on provider. The frontend behavior around flashing prices and connection status will be inconsistent unless event semantics are defined more tightly.

References:
- [PLAN.md:152](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L152)
- [PLAN.md:162](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L162)
- [PLAN.md:181](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L181)
- [PLAN.md:371](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L371)

Recommendation: define whether SSE emits only on real price changes, emits periodic heartbeats separately, or emits snapshots on a fixed cadence with a field indicating whether data is fresh.

### Medium: The plan omits executable ticker and pricing rules for trades

The API and chat flows both allow trade execution, but the plan does not define whether a user can trade symbols that are not already in the watchlist, what happens when a symbol has no current price in cache yet, or how unsupported symbols are rejected. Those rules are needed before backend and frontend implementations can converge on validation and error handling.

References:
- [PLAN.md:253](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L253)
- [PLAN.md:260](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L260)
- [PLAN.md:323](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L323)
- [PLAN.md:333](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L333)
- [PLAN.md:365](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L365)

Recommendation: add explicit rules for symbol normalization, tradable universe, required price availability, and the exact API/chat response when a trade cannot be priced or validated.

### Medium: The LLM integration section depends on a skill that is not part of this repo contract

The plan tells implementers to "use cerebras-inference skill" and references it multiple times, but the repository contract in `planning/` should stand on its own. A plan that requires an external agent skill without documenting the actual request shape, client setup, or fallback behavior is brittle for future contributors and for any execution environment where that skill is unavailable.

References:
- [PLAN.md:277](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L277)
- [PLAN.md:279](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L279)
- [PLAN.md:292](/home/lnayyar/projects/CourseAIVibeCodeUdmey/finally/planning/PLAN.md#L292)

Recommendation: move the concrete LiteLLM/OpenRouter request contract into the plan itself, including model string, provider selection, structured-output schema handling, and what to do if that exact setup is unavailable.
