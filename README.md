# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course.

## Features

- **Live price streaming** via SSE with green/red flash animations
- **Simulated portfolio** — $10k virtual cash, market orders, instant fills
- **Portfolio visualizations** — heatmap (treemap), P&L chart, positions table
- **AI chat assistant** — analyzes holdings, suggests and auto-executes trades
- **Watchlist management** — track tickers manually or via AI
- **Dark terminal aesthetic** — Bloomberg-inspired, data-dense layout

## Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite with lazy initialization
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

## Quick Start

```bash
# Clone and configure
cp .env.example .env
# Add your OPENROUTER_API_KEY to .env

# Run with Docker
docker build -t finally .
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally

# Open http://localhost:8000
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key for AI chat |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Project Structure

```
finally/
├── frontend/    # Next.js static export
├── backend/     # FastAPI uv project
├── planning/    # Project documentation and agent contracts
├── test/        # Playwright E2E tests
├── db/          # SQLite volume mount (runtime)
└── scripts/     # Start/stop helpers
```

## Development Workflow

This repo uses Claude Code subagents to keep planning docs and changes reviewed:

- **`.claude/commands/doc-review.md`** — a `/doc-review` slash command that appends a dated "Doc Review" section to a planning doc (questions, clarifications, simplification opportunities) without duplicating prior review rounds.
- **`.claude/agents/change-reviewer.md`** — a subagent that reviews all changes since the last commit.
- **Stop hook** (`.claude/settings.json`) — after each Claude Code session, appends a `git diff HEAD --stat` summary to `planning/PLAN.md` under an "Automated Change Review" heading.

## Notes for Stability & Cleanliness

- **`planning/PLAN.md` is growing unboundedly.** The Stop hook appends a diff-stat block every time a session ends, even across multiple stops with no new commits in between. Consider redirecting that output to a separate `planning/CHANGELOG.md` (or gating the hook on a non-empty diff) so the core spec document doesn't accumulate log noise.
- **No auth on a plan intended for optional public cloud deployment** (see `planning/PLAN.md` §11, §14): trade and chat endpoints are unauthenticated by design (single hardcoded `user_id`). Fine for local/course use — but if this is ever deployed to a public App Runner/Render URL, anyone with the link can trade the virtual portfolio and consume the deployer's OpenRouter quota. Worth a login gate or shared secret before any public deployment.
- **Floating-point drift risk**: `cash_balance`, `avg_cost`, and `quantity` are all `REAL` columns accumulating many small trades. Pin down a rounding convention (e.g., 2 decimals for cash/price) before the portfolio math package is built, so it isn't retrofitted later.
- **`codex` CLI dependency**: an earlier review pass installed `@openai/codex` under a user-owned npm prefix (`~/.npm-global`) because global installs needed `sudo`. That account is currently over its usage quota. If codex-based reviews are meant to be part of the standard workflow, document the install path and quota expectations somewhere in `planning/` so it's reproducible for other contributors.

## License

See [LICENSE](LICENSE).
