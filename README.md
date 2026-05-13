# FinAlly — AI Trading Workstation

A visually stunning, AI-powered trading terminal. Stream live market data, manage a simulated portfolio, and chat with an AI assistant that can analyze positions and execute trades on your behalf.

![Dark terminal aesthetic inspired by Bloomberg]

## Features

- **Live price streaming** — prices flash green/red on every tick via SSE
- **Sparkline mini-charts** — per-ticker price history built in real time from the stream
- **Simulated portfolio** — $10,000 virtual cash, instant market orders, no fees
- **Portfolio heatmap** — treemap sized by position weight, colored by P&L
- **AI chat assistant** — ask about your portfolio; the AI can execute trades and manage your watchlist through natural language
- **Watchlist management** — add/remove tickers manually or via AI chat

## Quick Start

```bash
# Copy environment config
cp .env.example .env
# Add your OpenRouter API key to .env (required for AI chat)
# MASSIVE_API_KEY is optional — simulator runs by default

# Build and run
docker build -t finally .
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

Open [http://localhost:8000](http://localhost:8000).

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter key for LLM chat |
| `MASSIVE_API_KEY` | No | Real market data via Massive API (simulator used if absent) |
| `LLM_MOCK` | No | Set `true` for deterministic mock responses (testing) |

## Architecture

```
Single container on port 8000
├── FastAPI — REST + SSE endpoints
├── Next.js static export — served by FastAPI
├── SQLite — zero-config persistence (volume-mounted)
└── LiteLLM → OpenRouter (Cerebras) — structured AI responses
```

- **Frontend**: Next.js + TypeScript (static export, no CORS)
- **Backend**: FastAPI, managed with `uv`
- **Real-time**: Server-Sent Events (`/api/stream/prices`)
- **Market data**: GBM simulator (default) or Massive REST API

## Development

### Backend
```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

## Testing

```bash
# Unit tests
cd backend && uv run pytest
cd frontend && npm test

# E2E (Playwright, requires Docker)
cd test && docker compose -f docker-compose.test.yml up
```

## License

MIT
