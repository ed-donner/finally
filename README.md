# FinAlly — AI Trading Workstation

An AI-powered trading workstation that streams live market data, supports simulated portfolio trading, and includes an LLM chat assistant that can analyze positions and execute trades on your behalf. Built to look and feel like a Bloomberg terminal with an AI copilot.

> **Course project**: Built entirely by orchestrated AI coding agents, demonstrating how agentic AI can produce a production-quality full-stack application.

---

## What You Get

- **Live price streaming** — prices flash green/red on every tick via SSE
- **Sparkline mini-charts** — per-ticker price action accumulated from the live stream
- **Buy/sell shares** — market orders, instant fill, no fees
- **Portfolio heatmap** — treemap sized by weight, colored by P&L
- **P&L chart** — total portfolio value over time, live-updating
- **Positions table** — quantity, avg cost, current price, unrealized P&L
- **AI chat** — ask questions, get analysis, have the AI execute trades and manage your watchlist
- **$10,000 virtual cash** — no login, no signup, start immediately

---

## Quick Start

```bash
# Copy and edit environment variables
cp .env.example .env
# Add your CEREBRAS_API_KEY to .env

# Start (macOS/Linux)
./scripts/start_mac.sh

# Start (Windows)
./scripts/start_windows.ps1
```

Open [http://localhost:8000](http://localhost:8000).

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CEREBRAS_API_KEY` | Yes | LLM inference via Cerebras |
| `MASSIVE_API_KEY` | No | Real market data via Massive/Polygon. Uses simulator if absent. |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing/CI) |

---

## Architecture

Single Docker container, single port (8000):

- **Frontend**: Next.js (TypeScript), static export served by FastAPI
- **Backend**: FastAPI (Python/uv)
- **Database**: SQLite — zero config, auto-initialized on first run
- **Real-time**: Server-Sent Events (SSE)
- **AI**: LiteLLM → Cerebras (`qwen-3-235b-a22b-instruct-2507`)
- **Market data**: Built-in GBM simulator (default) or Massive REST API

```
finally/
├── frontend/        # Next.js TypeScript project
├── backend/         # FastAPI uv project
├── planning/        # Project documentation
├── scripts/         # Start/stop scripts
├── test/            # Playwright E2E tests
├── db/              # SQLite volume mount (runtime only)
├── Dockerfile
└── docker-compose.yml
```

---

## Development

```bash
# Backend (requires uv)
cd backend
uv run uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend
npm install
npm run dev
```

---

## Testing

```bash
# Backend unit tests
cd backend && uv run pytest

# E2E tests (requires Docker)
cd test && docker compose -f docker-compose.test.yml up --abort-on-container-exit
```
