# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language. Think Bloomberg terminal meets AI copilot.

Built entirely by coding agents as the capstone project for an agentic AI coding course.

## Features

- **Live streaming prices** — real-time SSE-powered price updates with green/red flash animations
- **Simulated trading** — $10k virtual cash, market orders, instant fills
- **AI chat assistant** — analyze your portfolio, get trade suggestions, and execute trades through conversation
- **Portfolio visualization** — heatmap, P&L chart, positions table
- **Sparkline mini-charts** — inline price history for each ticker in the watchlist
- **Dark terminal aesthetic** — data-dense, professional layout inspired by Bloomberg

## Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) — REST API, SSE streaming, LLM integration
- **Database**: SQLite (volume-mounted for persistence)
- **Market Data**: Built-in GBM simulator (default) or real data via Massive/Polygon.io API
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs

## Quick Start

### Prerequisites

- Docker
- An [OpenRouter API key](https://openrouter.ai/) (for AI chat)

### Setup

```bash
# Clone the repo
git clone https://github.com/ed-donner/finally.git
cd finally

# Configure environment
cp .env.example .env
# Edit .env and add your OPENROUTER_API_KEY

# Start the app
./scripts/start_mac.sh        # macOS/Linux
# or
./scripts/start_windows.ps1   # Windows
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

### Stop

```bash
./scripts/stop_mac.sh         # macOS/Linux
./scripts/stop_windows.ps1    # Windows
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key for AI chat |
| `MASSIVE_API_KEY` | No | Polygon.io key for real market data (simulator used if absent) |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Project Structure

```
finally/
├── frontend/          # Next.js static export
├── backend/           # FastAPI + uv project
│   └── app/market/    # Market data subsystem (simulator, Massive client, SSE)
├── planning/          # Project docs and specs
├── scripts/           # Start/stop scripts
├── test/              # Playwright E2E tests
├── db/                # SQLite volume mount point
└── Dockerfile         # Multi-stage build (Node → Python)
```

## Development

### Backend

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

### Market Data Demo

```bash
cd backend
uv run market_data_demo.py
```

Displays a live Rich terminal dashboard with 10 tickers, sparklines, and price events for 60 seconds.

### Tests

```bash
cd backend
uv run pytest               # Unit tests
uv run pytest --cov=app     # With coverage
```

## Current Status

- **Market data subsystem** — complete (simulator, Massive client, SSE streaming, 73 tests passing)
- **Frontend, trading, portfolio, AI chat** — in development

## License

See [LICENSE](LICENSE) for details.
