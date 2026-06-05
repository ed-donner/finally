# FinAlly — AI Trading Workstation

A Bloomberg-inspired trading terminal with live streaming market data and an AI assistant that can analyze your portfolio and execute trades via natural language.

## Quick Start

```bash
# Copy and configure environment variables
cp .env.example .env

# Start the app (single Docker container)
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

Open [http://localhost:8000](http://localhost:8000). No login required — you start with $10,000 in virtual cash.

## Features

- Live-streaming prices with green/red flash animations and sparkline mini-charts
- Buy/sell via market orders — instant fill, no confirmation dialog
- Portfolio heatmap (treemap) sized by weight, colored by P&L
- P&L chart tracking total portfolio value over time
- AI chat assistant that analyzes positions and executes trades on your behalf

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter key for LLM chat |
| `MASSIVE_API_KEY` | No | Polygon.io key for real market data; omit to use the built-in simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock responses (testing/CI) |

## Architecture

Single container, port 8000:

- **Frontend**: Next.js static export served by FastAPI
- **Backend**: FastAPI (Python/uv), SQLite
- **Real-time**: Server-Sent Events (`GET /api/stream/prices`)
- **Market data**: GBM simulator by default; Polygon.io REST polling if `MASSIVE_API_KEY` is set
- **LLM**: LiteLLM → OpenRouter (Cerebras inference) with structured JSON outputs

## Development

```bash
# Backend
cd backend && uv sync --extra dev && uv run pytest -v

# Frontend
cd frontend && npm install && npm run dev
```

## Simulator Tickers

AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX, AMD, INTC, PYPL, UBER, LYFT, COIN, SNAP, SPOT, PLTR, RBLX
