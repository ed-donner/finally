# FinAlly — Agent Build Process

This file holds the instructions for coding agents that build FinAlly. The product is specified in [`PLAN.md`](PLAN.md). The HTTP and SSE contract is in [`API.md`](API.md). This file says how agents work together, and nothing about the product itself.

## How Agents Coordinate

- `backend/` and `planning/archive/` were permanently deleted on purpose. Build `backend/` from `PLAN.md` and `API.md`. Do not restore old code or docs from git history.
- Agents coordinate through files in `planning/`. Chat messages between agents are not a source of truth.
- `PLAN.md` describes behavior. `API.md` describes the wire contract. When the two disagree, stop and reconcile them before writing code.
- Changing the contract is a two-step process: update `API.md` (and `PLAN.md` if behavior changes) first, then the code. Never change the code's shape and leave the docs behind.
- Each agent owns one directory: frontend agent owns `frontend/`, backend and market-data agents own `backend/`, the test agent owns `test/`. Cross-directory changes go through the owning agent or through a contract change.

## Skills to Use

- **LLM calls:** when writing code that calls an LLM, use the `cerebras-inference` skill. The required path is LiteLLM → OpenRouter → Cerebras, with model `openrouter/openai/gpt-oss-120b`. Use structured outputs to interpret results. The key is in `OPENROUTER_API_KEY` in the project-root `.env`.
- **Charts:** when building any chart, use the `dataviz` skill for palette and layout decisions, then apply the color tokens in `PLAN.md` Section 2.

## Verification Before Handoff

An agent does not hand work to another agent or declare a task complete until:

1. The affected unit tests pass, and the output is reported (not summarized as "passing").
2. Any external-service assumption it relies on is either verified against live docs or listed as an open spike in `PLAN.md` Section 13.5.
3. For API changes, `API.md` matches the implemented status codes, field names, and error codes. The test suite should check this for every endpoint.

## Engineering Rules

- **One backend worker.** The price cache, trade lock, and SSE broadcaster are in-process. Do not add workers without moving that state out first.
- **Trades go through one code path**, whether the request came from the UI or the LLM. Do not add a second.
- **Secrets never reach the browser.** The API key is read server-side only. Do not add `NEXT_PUBLIC_` variables for secrets.
- **Escape all rendered text.** LLM output and user input are data, never HTML.
- **Scripts are idempotent** (definition in `PLAN.md` Section 11). Do not add commands that delete data.
- **Do not commit `.env`, `db/finally.db`, or `node_modules`.** The committed template is `.env.example`.
- **Keep the product non-goals.** `PLAN.md` Section 2 lists features that are out of scope. Do not build them without a contract change.

## Commit and PR Conventions

- Commit messages are imperative and describe the change in the first line, under 72 characters.
- Spec-only changes (anything under `planning/`) get their own commit, separate from code.
