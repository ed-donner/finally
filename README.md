# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course. See [planning/PLAN.md](planning/PLAN.md) for the full spec.

## Status

🚧 **Under active development.** Only the market data backend is built so far. The API routes, portfolio/trading logic, AI chat, frontend, and Docker packaging described below are planned but not yet implemented.

**Done:**
- ✅ Market data subsystem (`backend/app/market/`) — GBM simulator with correlated moves, Massive (Polygon.io) client, thread-safe price cache, SSE stream endpoint factory. 73 tests passing. See [planning/MARKET_DATA_SUMMARY.md](planning/MARKET_DATA_SUMMARY.md).

**Not yet started:**
- Database schema, portfolio/trade endpoints, watchlist endpoints
- AI chat integration (LiteLLM → OpenRouter via Cerebras)
- Frontend (Next.js terminal UI)
- Dockerfile, start/stop scripts, E2E tests

## Planned Features

- **Live price streaming** via SSE with green/red flash animations
- **Simulated portfolio** — $10k virtual cash, market orders, instant fills
- **Portfolio visualizations** — heatmap (treemap), P&L chart, positions table
- **AI chat assistant** — analyzes holdings, suggests and auto-executes trades
- **Watchlist management** — track tickers manually or via AI
- **Dark terminal aesthetic** — Bloomberg-inspired, data-dense layout

## Planned Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite with lazy initialization
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional) — ✅ implemented

## Try the Market Data Demo

The only runnable piece right now is a terminal demo of the market data simulator:

```bash
cd backend
uv sync
uv run market_data_demo.py
```

Displays a live-updating dashboard with all 10 tickers, sparklines, and an event log. Runs 60 seconds or until Ctrl+C.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes (once chat is built) | OpenRouter API key for AI chat |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

## Project Structure

```
finally/
├── backend/     # FastAPI uv project (market data subsystem built; API/DB/chat pending)
├── planning/    # Project documentation and agent contracts
└── (planned)    frontend/, test/, db/, scripts/
```

## License

See [LICENSE](LICENSE).
