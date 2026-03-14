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
- **Database**: SQLite with lazy initialization
- **AI**: LiteLLM → Google Gemini with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

## Quick Start

```bash
# Clone and configure
cp .env.example .env
# Add your GOOGLE_API_KEY to .env

# Run with Docker
docker build -t finally .
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally

# Open http://localhost:8000
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_API_KEY` | Yes | Google API key for AI chat (Gemini) |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Project Structure

```
finally/
├── frontend/    # Next.js static export
├── backend/     # FastAPI uv project
├── planning/    # Project documentation and agent contracts
├── test/        # Playwright E2E tests
├── db/          # SQLite volume mount (runtime)
└── scripts/     # Start/stop helpers
```

## License

See [LICENSE](LICENSE).
