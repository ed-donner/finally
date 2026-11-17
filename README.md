# FinAlly — AI Trading Workstation

FinAlly (Finance Ally) is a simulated trading terminal in the style of a Bloomberg workstation. It streams live prices, lets you trade a $10,000 virtual portfolio, and includes an AI copilot that can analyze your positions and place trades for you in plain English.

It is the capstone project for an agentic AI coding course. Coding agents build it, coordinating through the docs in `planning/`.

> **Status:** the market data subsystem is complete (`backend/app/market/`). The rest of the platform is in progress. See [`planning/PLAN.md`](planning/PLAN.md) for the full spec.

## Features

- Live price streaming over SSE, with green/red flash animations and sparklines
- Market orders with instant fills and fractional shares
- Portfolio heatmap, P&L chart and positions table
- AI chat assistant (LLM via OpenRouter/Cerebras) that can execute trades and manage the watchlist
- Built-in market simulator by default, or real data from Massive (Polygon.io) if you supply an API key

## Tech Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js + TypeScript (static export), Tailwind CSS, Recharts |
| Backend | FastAPI (Python 3.12, managed with `uv`) |
| Database | SQLite at `db/finally.db`, created and seeded automatically |
| Real-time | Server-Sent Events (`/api/stream/prices`) |
| AI | LiteLLM → OpenRouter (`openai/gpt-oss-120b` on Cerebras) |
| Deployment | One Docker container serving everything on port 8000 |

## Quick Start

```bash
cp .env.example .env        # add your OPENROUTER_API_KEY
./scripts/start.sh          # Windows: ./scripts/start.ps1
# open http://localhost:8000
./scripts/stop.sh           # data in db/ persists
```

To reset the app, stop the container and delete `db/finally.db`.

### Environment Variables

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Enables the AI chat. Without it, the app still runs but chat replies that the AI is not configured. |
| `MASSIVE_API_KEY` | Optional. Uses real market data instead of the simulator. |
| `LLM_MOCK` | `true` returns deterministic mock LLM responses (for tests). |
| `DB_PATH` | Optional SQLite path override. |

## Development

```bash
cd backend
uv sync
uv run pytest                    # backend tests
uv run market_data_demo.py       # terminal demo of the price simulator
```

E2E tests (Playwright) live in `test/` and run through `test/docker-compose.test.yml` with `LLM_MOCK=true`.

## Project Layout

```
frontend/    Next.js app (static export)
backend/     FastAPI uv project (market data, API, DB, LLM)
planning/    Specs and agent docs: start with PLAN.md
scripts/     Start/stop scripts for Docker
test/        Playwright E2E tests
db/          Runtime SQLite data (bind-mounted)
```

## Disclaimer

The app has no authentication, and all trading is simulated with fake money. Don't expose a public instance: anyone who reaches it can use your OpenRouter credits.

## License

See [LICENSE](LICENSE).
