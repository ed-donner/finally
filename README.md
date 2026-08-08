# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course. Full spec: [`planning/PLAN.md`](planning/PLAN.md).

## Features

- **Live price streaming** via SSE with green/red flash animations
- **Simulated portfolio** — $10k virtual cash, market orders, instant fills
- **Portfolio visualizations** — heatmap (treemap), P&L chart, positions table
- **AI chat assistant** — analyzes holdings, suggests and auto-executes trades
- **Watchlist management** — track tickers manually or via AI (20-ticker cap)
- **Dark terminal aesthetic** — Bloomberg-inspired, data-dense layout

## Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite in a named Docker volume, schema initialized on startup
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

## Quick Start

```bash
cp .env.example .env
# Add your OPENROUTER_API_KEY to .env

./scripts/start_mac.sh        # macOS/Linux
# or
./scripts/start_windows.ps1   # Windows PowerShell

# Open http://localhost:8000
```

Both scripts wrap `docker compose up -d --build`; stop with `scripts/stop_mac.sh` / `scripts/stop_windows.ps1` (data persists in the `finally-data` volume).

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key for AI chat |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use the simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |
| `MARKET_TICK_MS` | No | Market data poll/tick interval; defaults to 500ms (simulator) / 15000ms (Massive) |
| `APP_SHARED_SECRET` | No | Shared secret required on all `/api/*` requests; **required** for any non-localhost deployment |

See [`.env.example`](.env.example) for the full template.

## Project Structure

```
finally/
├── frontend/    # Next.js static export
├── backend/     # FastAPI uv project (owns DB schema, API, SSE, market data, LLM)
├── planning/    # Project documentation and agent contracts
├── test/        # Playwright E2E tests + docker-compose.test.yml
└── scripts/     # Start/stop helpers wrapping docker compose
```

The SQLite database lives in a named Docker volume (`finally-data`), not in the repo tree.

## Testing

- Backend: `pytest` (within `backend/`)
- Frontend: unit tests (within `frontend/`)
- E2E: Playwright suite in `test/`, run against `docker-compose.test.yml` with `LLM_MOCK=true`

## License

See [LICENSE](LICENSE).
