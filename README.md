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
- **Database**: SQLite with lazy initialization (`/app/db/finally.db`, persisted in the `finally-data` volume)
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

## Quick Start

Requires Docker.

```bash
cp .env.example .env          # then set OPENROUTER_API_KEY in .env
./scripts/start_mac.sh        # macOS / Linux
# .\scripts\start_windows.ps1 # Windows PowerShell
```

Open http://localhost:8000. The start script builds the image if needed, (re)starts the `finally` container, waits for `/api/health` and opens your browser.

| Command | Description |
|---|---|
| `./scripts/start_mac.sh --build` | Force an image rebuild (Windows: `-Build`) |
| `./scripts/start_mac.sh --no-open` | Don't open the browser (Windows: `-NoOpen`) |
| `./scripts/stop_mac.sh` | Stop and remove the container; data volume is kept |
| `docker volume rm finally-data` | Reset all data (cash, positions, chat history) |

All scripts are idempotent. If `.env` is missing, the start script creates it from `.env.example`.

### Docker Compose / plain Docker

```bash
docker compose up -d --build     # build + run
docker compose down              # stop (volume kept)

# or by hand
docker build -t finally .
docker run -d --name finally -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes (for chat) | OpenRouter API key for AI chat |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use the simulator |
| `LLM_MOCK` | No | `true` for deterministic mock LLM responses (testing, no API key needed) |

Set inside the image (override only if you know why): `FINALLY_DB_PATH=/app/db/finally.db`, `FINALLY_STATIC_DIR=/app/backend/static`.

## Local Development

Backend (http://localhost:8000, reads `.env` from the repo root, DB at `db/finally.db`):

```bash
cd backend
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8000
```

Frontend (http://localhost:3000, proxies `/api/*` to the backend in dev):

```bash
cd frontend
npm install
npm run dev
```

## Tests

```bash
# Backend unit tests + lint
cd backend && uv run --extra dev pytest && uv run --extra dev ruff check app tests

# Frontend unit tests
cd frontend && npm test

# E2E (Playwright, app runs with LLM_MOCK=true)
docker compose -f test/docker-compose.test.yml up --build --abort-on-container-exit
```

## Project Structure

```
finally/
├── frontend/    # Next.js static export
├── backend/     # FastAPI uv project
├── planning/    # Project documentation and agent contracts
├── test/        # Playwright E2E tests
├── db/          # SQLite volume mount (runtime)
├── scripts/     # Start/stop helpers
├── Dockerfile   # Multi-stage build (Node → Python)
└── docker-compose.yml
```

## License

See [LICENSE](LICENSE).
