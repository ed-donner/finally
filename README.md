# FinAlly — AI Trading Workstation

A visually striking AI-powered trading workstation with live market data, simulated portfolio management, and an LLM chat assistant that can analyze positions and execute trades. Looks and feels like a Bloomberg terminal with an AI copilot.

## Features

- Live streaming prices with green/red flash animations
- Sparkline mini-charts built from the SSE stream
- $10,000 virtual cash to trade with (market orders, instant fill)
- Portfolio heatmap (treemap) and P&L chart
- AI chat assistant — ask questions, get analysis, execute trades via natural language

## Quick Start

```bash
# Copy and fill in your API key
cp .env.example .env

# Start (macOS/Linux)
./scripts/start_mac.sh

# Start (Windows PowerShell)
./scripts/start_windows.ps1
```

Open [http://localhost:8000](http://localhost:8000).

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter key for LLM chat |
| `MASSIVE_API_KEY` | No | Polygon.io key for real market data (simulator used if absent) |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Architecture

Single Docker container on port 8000:

- **Frontend**: Next.js (TypeScript), built as a static export, served by FastAPI
- **Backend**: FastAPI (Python/uv)
- **Database**: SQLite, volume-mounted at `db/finally.db`
- **Real-time**: Server-Sent Events (SSE) for price streaming
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs

## Development

```bash
# Backend
cd backend && uv sync --extra dev
uv run pytest -v

# Frontend
cd frontend && npm install
npm run dev
```

## Testing

E2E tests use Playwright with `LLM_MOCK=true`:

```bash
cd test && docker compose -f docker-compose.test.yml up
```
