# FinAlly — AI Trading Workstation

A Bloomberg-terminal-inspired trading simulator with live market data and an AI assistant that can analyze your portfolio and execute trades via natural language.

## Quick Start

```bash
cp .env.example .env
# Edit .env: add OPENROUTER_API_KEY (required), MASSIVE_API_KEY (optional)
./scripts/start_mac.sh
```

Open [http://localhost:8000](http://localhost:8000).

## Features

- **Live price streaming** via SSE — prices flash green/red on change
- **Simulated portfolio** — $10k virtual cash, market orders, instant fill
- **Sparklines & charts** — per-ticker mini-charts and a detailed main chart
- **Portfolio heatmap** — treemap sized by weight, colored by P&L
- **AI chat** — ask questions, get analysis, execute trades in plain English

## Architecture

Single Docker container, single port (8000):

- **Frontend**: Next.js static export, served by FastAPI
- **Backend**: FastAPI + Python (uv), SQLite database
- **Market data**: GBM simulator by default; Massive/Polygon.io API if `MASSIVE_API_KEY` is set
- **AI**: LiteLLM → OpenRouter (Cerebras), structured outputs for trade execution

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | LLM inference via OpenRouter |
| `MASSIVE_API_KEY` | No | Real market data; simulator used if unset |
| `LLM_MOCK` | No | Set `true` for deterministic mock responses (testing) |

## Development

```bash
# Backend tests
cd backend && uv run pytest -v

# Frontend
cd frontend && npm install && npm run dev
```

## Running Tests (E2E)

```bash
cd test && docker compose -f docker-compose.test.yml up
```
