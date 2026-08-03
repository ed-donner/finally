# FinAlly — AI Trading Workstation

A Bloomberg-style trading terminal with an AI copilot: live streaming prices, a simulated
$10k portfolio, and an LLM chat assistant that can analyse positions and execute trades from
natural language.

Built entirely by coding agents as the capstone project for an agentic AI coding course.
Agents coordinate through the shared documents in `planning/`.

## Status

Market data subsystem is complete and tested (73 tests). The rest of the platform is in
development.

| Component | State |
|---|---|
| Market data (simulator, Massive client, price cache, SSE stream) | Built |
| Database, portfolio and watchlist APIs | Planned |
| Next.js frontend, charts, chat panel | Planned |
| Docker image and start/stop scripts | Planned |

## Architecture

One container, one port. FastAPI serves the REST API, the SSE price stream, and the exported
Next.js frontend as static files on port 8000.

- **Backend** — FastAPI, Python 3.12, managed with `uv`
- **Frontend** — Next.js static export, TypeScript, Tailwind, Recharts
- **Database** — SQLite at `db/finally.db`, lazily created and seeded
- **Real-time** — Server-Sent Events, one event per tick carrying every tracked ticker
- **AI** — LiteLLM to OpenRouter (Cerebras inference) with structured outputs
- **Market data** — built-in GBM simulator by default, Massive (Polygon.io) if a key is set

## Running the Market Data Demo

A Rich terminal dashboard of the live simulated price stream:

```bash
cd backend
uv sync --dev
uv run market_data_demo.py
```

## Tests

```bash
cd backend
uv run pytest
uv run ruff check .
```

## Environment Variables

Create `.env` in the project root. Every variable is optional — with none set, the app runs on
the simulator and chat reports that no API key is configured.

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | Enables the AI chat panel |
| `MASSIVE_API_KEY` | Use real market data instead of the simulator |
| `LLM_MOCK` | Set `true` for deterministic mock LLM responses in tests |

## Project Structure

```
finally/
├── backend/     # FastAPI uv project (app/market/ is built)
├── frontend/    # Next.js static export
├── planning/    # PLAN.md and agent reference docs
├── test/        # Playwright E2E tests, run on the host
├── scripts/     # Docker start/stop helpers
└── db/          # SQLite bind mount at runtime
```

## Documentation

- `planning/PLAN.md` — the full specification and the authority for all agent work
- `planning/MARKET_DATA_SUMMARY.md` — what the market data subsystem does and how

## License

See [LICENSE](LICENSE).
