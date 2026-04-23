# Uncommitted Code Review

Reviewed the full worktree against `HEAD` (`14550e1`) on 2026-04-19, including staged, unstaged, and untracked files.

## Findings

1. High: the latest user turn is sent to the LLM twice on every chat request. `handle_user_message()` persists the user message first, then `_build_messages()` reloads recent history and appends the same `user_message` again, so the prompt contains a duplicated final user turn. That biases model behavior and makes approval-gated execution prompts harder to reason about. References: `backend/app/llm/service.py:33-35`, `backend/app/llm/service.py:126-129`.

2. High: watchlist persistence and live market-source sync are not atomic. Both the HTTP routes and the LLM action path update SQLite first and only then call `market_source.add_ticker()` / `remove_ticker()`. If the market source raises, the request fails after the database has already changed, leaving the saved watchlist and live stream state out of sync until restart or manual repair. References: `backend/app/watchlist_api.py:66-72`, `backend/app/watchlist_api.py:84-90`, `backend/app/llm/service.py:97-111`.

3. High: trade execution is split across multiple autocommitted SQLite connections, so a mid-flight write failure can corrupt state. `execute_trade()` updates positions, cash balance, trade log, and snapshots through separate repository calls, while `connect()` opens SQLite with `isolation_level=None`. If any later step fails, earlier writes are already committed. The obvious bad case is position/cash mutation succeeding while the trade log insert fails. References: `backend/app/db/connection.py:24`, `backend/app/portfolio/service.py:117-135`.

4. Medium: `npm run typecheck` is not runnable from a clean checkout. `frontend/tsconfig.json` includes `.next/types/**/*.ts`, but those files do not exist until after a Next build. I reproduced this: `npm run typecheck` failed before `npm run build`, then passed after build output generated the missing files. That makes the standalone typecheck command unreliable for CI and local development. Reference: `frontend/tsconfig.json:19`.

5. Medium: the frontend dev API default points at an undocumented backend port. When the app runs on `localhost:3000`, `API_BASE` falls back to `http://127.0.0.1:8001`, but the repo docs only describe port `8000` and there is no matching backend dev configuration in the checked-in docs. A default local setup will therefore fail unless the developer knows to override `NEXT_PUBLIC_API_BASE`. References: `frontend/lib/api.ts:13-17`, `README.md:18-37`, `backend/README.md:47-58`.

6. Medium: the main page layout is desktop-only despite the project requirements calling for desktop and mobile support. The shell hard-codes a three-column grid with minimum widths of `280px`, `320px`, and `320px`, plus a `380px` order-ticket column inside the positions area, and there is no responsive fallback. On narrow screens this will overflow rather than reflow. References: `frontend/app/page.tsx:39-47`, `frontend/app/page.tsx:58-65`.

7. Medium: the worktree includes a large amount of generated and runtime output, and the top-level `.gitignore` does not cover most of it. Current untracked content includes `frontend/node_modules/`, `frontend/.next/`, `frontend/out/`, `.playwright-mcp/`, backend `__pycache__` trees, `backend/db/finally.db`, and multiple PNG screenshots. Even if these are not committed now, the repo is one `git add .` away from a massive noisy commit and binary/runtime data leaking into version control. Reference: `.gitignore` plus current `git status --short`.

## Tests Run

- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest` in `backend/`: `168 passed`.
- `npm run build` in `frontend/`: succeeded.
- `npm run typecheck` in `frontend/`: failed before build because `.next/types/...` files were missing, then passed after `npm run build`.

## Coverage And Style Gaps

- `backend/tests/llm/test_service.py:106-116` checks that prior history is included in the prompt, but there is no test covering the real request path where the new user message has already been persisted. That is why the duplicate-turn bug currently slips through.
- There are no failure-injection tests around `market_source.add_ticker()` / `remove_ticker()` raising after the database mutates.
- There are no failure-injection tests around partial database write failures inside `execute_trade()`, so the transactional integrity issue is untested.
- The frontend change is large, but there are no frontend unit tests or E2E tests in this worktree to cover chat approval UX, manual trading, watchlist edits, SSE reconnects, or mobile layout behavior.

## Overall Risk

The backend test suite is strong on happy-path behavior and domain validation, but the current uncommitted changes still carry real correctness risk in three places: prompt construction, atomicity of state changes, and operational hygiene around generated artifacts. The frontend builds, but the local-dev path and responsive layout are not in a production-ready state yet.
