# FinAlly — AI Trading Workstation

FinAlly (Finance Ally) is a visually stunning, AI-powered trading workstation: it streams live market data, lets you trade a simulated portfolio, and includes an LLM chat assistant that can analyze your positions and execute trades on your behalf. Think Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course — built entirely by coding agents to demonstrate how orchestrated AI agents can produce a production-quality full-stack application.

## Status

The project is under active development. The **market data subsystem** (live-price simulator, Massive/Polygon.io client, price cache, SSE streaming) is complete and lives in `backend/app/market/`. The rest of the platform — portfolio/trading, watchlist, LLM chat, frontend, and Docker packaging — is still to be built.

See [`planning/PLAN.md`](planning/PLAN.md) for the full project specification, and [`planning/MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md) for details on the completed market data component.

## Architecture

A single Docker container, single port (8000):

- **Frontend**: Next.js + TypeScript, built as a static export, served by FastAPI
- **Backend**: FastAPI (Python), managed with `uv`
- **Database**: SQLite, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (SSE)
- **AI**: LiteLLM → OpenRouter (Cerebras inference), structured outputs for trade execution
- **Market data**: simulator by default, real data via the Massive API if a key is provided

Full rationale and details are in `planning/PLAN.md`.

## Getting Started (backend, current state)

```bash
cd backend
uv sync --extra dev        # install dependencies
uv run --extra dev pytest  # run tests
uv run market_data_demo.py # live terminal dashboard with simulated prices
```

See [`backend/CLAUDE.md`](backend/CLAUDE.md) and [`backend/README.md`](backend/README.md) for backend developer docs.

A one-command Docker launch (`scripts/start_mac.sh` / `scripts/start_windows.ps1`) is planned once the frontend and portfolio/chat backend are in place.

## Environment Variables

```bash
OPENROUTER_API_KEY=   # required for LLM chat
MASSIVE_API_KEY=      # optional; enables real market data instead of the simulator
LLM_MOCK=false        # set true for deterministic mock LLM responses (testing)
```

## Project Structure

```
finally/
├── frontend/     # Next.js TypeScript app (static export) — planned
├── backend/      # FastAPI uv project — market data implemented
├── planning/     # Project specification and design docs for agents
├── scripts/      # Docker start/stop scripts — planned
├── test/         # Playwright E2E tests — planned
└── db/           # SQLite volume mount point
```

## License

See [`LICENSE`](LICENSE).
