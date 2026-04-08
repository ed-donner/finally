# FinAlly — AI Trading Workstation

An AI-powered trading workstation with live market data, a simulated portfolio, and an LLM chat assistant that can analyze positions and execute trades. Built as a capstone project for an agentic AI coding course.

## What It Does

- Streams live prices for 10 default tickers (simulated by default, real data via Massive API)
- $10,000 virtual cash to trade with — buy/sell with market orders, instant fill
- Portfolio heatmap, P&L chart, positions table, and sparkline mini-charts
- AI chat assistant (powered by Cerebras via OpenRouter) that can discuss your portfolio and execute trades

## Stack

- **Frontend**: Next.js (TypeScript, static export), Tailwind CSS
- **Backend**: FastAPI (Python, managed by `uv`), SQLite
- **Real-time**: Server-Sent Events (SSE)
- **AI**: LiteLLM → OpenRouter → Cerebras (`openai/gpt-oss-120b`)
- **Deployment**: Single Docker container on port 8000

## Setup

1. Copy `.env.example` to `.env` and add your API key:
   ```
   OPENROUTER_API_KEY=your-key-here
   ```

2. Start:
   ```bash
   # macOS/Linux
   ./scripts/start_mac.sh

   # Windows
   .\scripts\start_windows.ps1
   ```

3. Open `http://localhost:8000`

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter key for LLM chat |
| `MASSIVE_API_KEY` | No | Real market data (omit to use simulator) |
| `LLM_MOCK` | No | Set `true` for deterministic mock responses (testing) |

## Development

```bash
cd backend
uv venv && uv pip install -e .
uv run uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install && npm run dev
```

## Testing

```bash
# Backend unit tests
cd backend && uv run pytest tests/ -v

# E2E tests (requires Docker)
cd test && docker compose -f docker-compose.test.yml up
```
