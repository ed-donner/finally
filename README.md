# FinAlly — AI Trading Workstation

A visually stunning, AI-powered trading workstation with live market data, simulated portfolio trading, and an LLM chat assistant that can analyze positions and execute trades on your behalf.

## Quick Start

```bash
cp .env.example .env
# Edit .env — add your OPENROUTER_API_KEY (required for AI chat)

# macOS / Linux
./scripts/start_mac.sh

# Windows PowerShell
./scripts/start_windows.ps1
```

Open [http://localhost:8000](http://localhost:8000). No login required.

## What You Get

- **Live price streaming** — 10 default tickers with green/red flash animations and sparklines
- **$10,000 virtual cash** — buy and sell shares at live market prices
- **Portfolio heatmap** — treemap sized by weight, colored by P&L
- **P&L chart** — portfolio value over time
- **AI chat assistant** — ask about your portfolio, get analysis, have the AI execute trades

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | For AI chat (via OpenRouter → Cerebras) |
| `MASSIVE_API_KEY` | No | Real market data from Polygon.io. Omit to use the built-in simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Architecture

Single Docker container on port 8000:

- **Frontend**: Next.js (TypeScript, static export) — served by FastAPI
- **Backend**: FastAPI (Python/uv) — REST + SSE streaming
- **Database**: SQLite (volume-mounted, auto-initialized on first start)
- **Market data**: GBM simulator by default; Polygon.io REST poller if API key provided
- **AI**: LiteLLM → OpenRouter → Cerebras (fast inference, structured outputs)

## Development

```bash
# Backend
cd backend
uv sync --dev
uv run pytest -v

# Frontend
cd frontend
npm install
npm run dev
```

## Testing

```bash
# Unit tests (backend)
cd backend && uv run pytest

# E2E tests (requires Docker)
cd test && docker-compose -f docker-compose.test.yml up
```
