---
phase: 05-llm-chat-integration
plan: 01
subsystem: llm
tags: [litellm, openrouter, cerebras, pydantic, structured-output, chat, mock]

# Dependency graph
requires:
  - phase: 02-portfolio-engine
    provides: execute_trade, get_portfolio, record_snapshot
  - phase: 03-watchlist-management
    provides: add_ticker, remove_ticker, get_watchlist
  - phase: 01-database-schema
    provides: chat_messages table, init_db
provides:
  - process_chat_message orchestrator for full chat flow
  - ChatRequest/ChatResponse Pydantic models
  - ChatLLMResponse structured output schema for LLM
  - build_system_prompt with live portfolio context
  - get_mock_response deterministic mock for LLM_MOCK=true
  - parse_llm_response with fallback for malformed output
  - save_chat_message/load_chat_history for persistence
affects: [05-02-chat-router, 06-frontend-chat, 10-docker]

# Tech tracking
tech-stack:
  added: [litellm>=1.81.10]
  patterns: [extra_body for OpenRouter structured output, defensive JSON parsing with fallback, error collection pattern for action execution]

key-files:
  created:
    - backend/app/llm/__init__.py
    - backend/app/llm/models.py
    - backend/app/llm/prompt.py
    - backend/app/llm/mock.py
    - backend/app/llm/service.py
    - backend/tests/llm/__init__.py
    - backend/tests/llm/conftest.py
    - backend/tests/llm/test_service.py
  modified:
    - backend/pyproject.toml

key-decisions:
  - "extra_body for response_format to bypass LiteLLM OpenRouter check"
  - "Defensive JSON parsing: model_validate_json with fallback to plain message"
  - "Error collection: failed trades/watchlist changes reported as results, not exceptions"
  - "MockMarketDataSource in test conftest tracks add/remove calls for assertions"

patterns-established:
  - "LLM structured output via extra_body with model_json_schema()"
  - "Action execution with error collection (try/except per action, collect results)"
  - "Mock mode via LLM_MOCK env var for deterministic testing"

# Metrics
duration: 4min
completed: 2026-02-11
---

# Phase 5 Plan 1: LLM Chat Service Layer Summary

**LLM chat orchestrator with Pydantic structured output, mock mode, and auto-execution of trades/watchlist changes through existing services**

## Performance

- **Duration:** 4min
- **Started:** 2026-02-11T18:35:46Z
- **Completed:** 2026-02-11T18:39:23Z
- **Tasks:** 2
- **Files modified:** 9

## Accomplishments
- Complete LLM service layer: models, prompt builder, mock mode, and orchestrator
- 18 unit tests covering parsing, mock responses, chat history, and full process flow
- 163 total backend tests passing (145 existing + 18 new)
- litellm dependency added with OpenRouter/Cerebras provider routing

## Task Commits

Each task was committed atomically:

1. **Task 1: Create LLM module -- models, prompt, mock, service** - `8e44ca1` (feat)
2. **Task 2: Write unit tests for LLM service layer** - `9eb79ae` (test)

## Files Created/Modified
- `backend/app/llm/__init__.py` - Public API exports (process_chat_message, ChatRequest, ChatResponse)
- `backend/app/llm/models.py` - Pydantic schemas: TradeAction, WatchlistAction, ChatLLMResponse, ChatRequest, TradeResult, WatchlistResult, ChatResponse
- `backend/app/llm/prompt.py` - build_system_prompt with live portfolio context and JSON schema instructions
- `backend/app/llm/mock.py` - Keyword-matched deterministic mock responses for LLM_MOCK=true
- `backend/app/llm/service.py` - process_chat_message orchestrator, parse_llm_response, save/load_chat_history
- `backend/tests/llm/__init__.py` - Test package marker
- `backend/tests/llm/conftest.py` - Fixtures: db, price_cache (AAPL/GOOGL/MSFT/PYPL), MockMarketDataSource
- `backend/tests/llm/test_service.py` - 18 tests covering parsing, mock, history, and full flow
- `backend/pyproject.toml` - Added litellm>=1.81.10 dependency

## Decisions Made
- Used extra_body for response_format to bypass LiteLLM's incorrect OpenRouter capability check (standard workaround per LiteLLM GitHub discussion #11652)
- Defensive JSON parsing: try model_validate_json first, fall back to plain message response for malformed LLM output
- Error collection pattern: each trade/watchlist action executed independently, failures reported as result entries rather than raising exceptions
- MockMarketDataSource in test conftest tracks add_ticker/remove_ticker calls in lists for assertion

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required. LLM_MOCK=true mode works without API keys.

## Next Phase Readiness
- LLM service layer complete, ready for Plan 02 (chat router + integration tests)
- process_chat_message is the single entry point for POST /api/chat
- Mock mode enables testing without OpenRouter API key

## Self-Check: PASSED

- All 8 created files exist on disk
- Commit 8e44ca1 (Task 1) found in git log
- Commit 9eb79ae (Task 2) found in git log
- 163 tests passing (145 existing + 18 new)
- Lint clean (ruff check passes)

---
*Phase: 05-llm-chat-integration*
*Completed: 2026-02-11*
