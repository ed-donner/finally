# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course.

## Features

- **Live price streaming** via SSE with green/red flash animations
- **Simulated portfolio** — $10k virtual cash, market orders, instant fills
- **Portfolio visualizations** — heatmap (treemap), P&L chart, positions table
- **AI chat assistant** — analyzes holdings, suggests and auto-executes trades
- **Watchlist management** — track tickers manually or via AI
- **Portfolio reset** — back to $10k cash and the default watchlist in one action
- **Dark terminal aesthetic** — Bloomberg-inspired, data-dense layout

## Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite, created and seeded at startup
- **AI**: Anthropic API (official Python SDK) with structured outputs, configured from `.env`
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

## Quick Start

```bash
# Clone and configure
cp .env.example .env
# Set ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL and ANTHROPIC_MODEL in .env

# Run with the start script (macOS/Linux)
scripts/start_mac.sh            # add --build to rebuild the image
# Windows: scripts\start_windows.ps1

# Or run Docker directly
docker build -t finally .
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally

# Open http://localhost:8000
```

Stop with `scripts/stop_mac.sh` (or `scripts\stop_windows.ps1`). The data volume is kept.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | For chat | Auth token for the Anthropic API (not needed with `LLM_MOCK=true`) |
| `ANTHROPIC_BASE_URL` | For chat | Anthropic-compatible API endpoint |
| `ANTHROPIC_MODEL` | For chat | Model name used for chat |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Project Structure

```
finally/
├── frontend/    # Next.js static export
├── backend/     # FastAPI uv project
├── planning/    # Project documentation and agent contracts
├── test/        # Playwright E2E tests (run from the host)
├── db/          # SQLite file for local non-Docker runs
└── scripts/     # Start/stop helpers
```

## Local Development

The Next.js dev server proxies `/api` to the backend on `localhost:8000` (via a Next.js rewrite), so run the backend first, then the frontend dev server.

## License

See [LICENSE](LICENSE).
