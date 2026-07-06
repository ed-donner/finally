# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course. See [`planning/PLAN.md`](planning/PLAN.md) for the full specification.

## Status

**In progress.** The market data subsystem is complete; the rest of the platform (portfolio, LLM chat, frontend, Docker packaging) is still to be built.

- ✅ Market data backend — GBM simulator + Massive (Polygon.io) client behind a shared interface, SSE price streaming, 73 tests passing. See [`planning/MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md).
- ⬜ Portfolio & trading (positions, trades, P&L)
- ⬜ AI chat assistant (LiteLLM → OpenRouter, Cerebras inference)
- ⬜ Frontend (Next.js trading terminal UI)
- ⬜ Docker packaging & start/stop scripts
- ⬜ E2E tests

## Architecture (target)

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite with lazy initialization
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

## Backend (available now)

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -v          # run tests
uv run market_data_demo.py            # live terminal dashboard of simulated prices
```

See [`backend/README.md`](backend/README.md) and [`backend/CLAUDE.md`](backend/CLAUDE.md) for details on the market data API.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | For AI chat | OpenRouter API key |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use the simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Project Structure

```
finally/
├── backend/     # FastAPI uv project (market data complete; portfolio/AI/API pending)
├── planning/    # Project documentation and agent contracts
└── db/          # SQLite volume mount (runtime)
```

`frontend/`, `scripts/`, and `test/` are not yet created — see `planning/PLAN.md` for their intended layout.

## License

See [LICENSE](LICENSE).
