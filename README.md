# FinAlly — AI Trading Workstation

A Bloomberg-style trading terminal with live prices, a simulated $10,000 portfolio, and an AI assistant that can analyze positions and execute trades from chat. Built entirely by coding agents as the capstone for an agentic AI coding course.

## Status

Work in progress. The market data subsystem (simulator, Massive client, price cache, SSE stream) is complete and tested. The rest of the backend, the frontend, the Dockerfile and the start scripts are still to be built — see [`planning/PLAN.md`](planning/PLAN.md) for the full specification.

## Architecture

One Docker container on port 8000:

- **Frontend**: Next.js (TypeScript, Tailwind) static export, served by FastAPI
- **Backend**: FastAPI, managed with `uv`
- **Database**: SQLite, created and seeded automatically
- **Real-time**: Server-Sent Events at `/api/stream/prices`
- **AI**: LiteLLM → OpenRouter (`openai/gpt-oss-120b` on Cerebras) with structured outputs
- **Market data**: built-in GBM simulator by default, Massive API if a key is set

## Environment Variables

Set these in `.env` in the project root:

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter key for the AI chat |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; leave empty to use the simulator |
| `LLM_MOCK` | No | `true` returns deterministic mock LLM responses (for tests) |

## Development

What runs today is the backend's market data code:

```bash
cd backend
uv sync --dev
uv run pytest                    # tests
uv run ruff check .              # lint
uv run python market_data_demo.py   # live terminal demo of the simulator
```

Once the app is built, it will launch with `scripts/start_mac.sh` or `scripts/start_windows.ps1` and be served at http://localhost:8000.

## Project Structure

```
finally/
├── backend/     # FastAPI uv project (market data done)
├── planning/    # Specification and agent reference docs
├── frontend/    # Next.js app (planned)
├── scripts/     # Start/stop scripts (planned)
└── test/        # Playwright E2E tests (planned)
```

## License

MIT — see [LICENSE](LICENSE).
