# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that will stream live market data, simulate portfolio trading, and integrate an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course. Full specification: [`planning/PLAN.md`](planning/PLAN.md).

## Status

🚧 In progress. The **market data subsystem** (GBM simulator, price cache, SSE streaming interface) is built and tested in `backend/`. The FastAPI app, database, LLM chat integration, Next.js frontend, and Docker packaging described in the plan are not yet implemented.

## Planned Features

- Live price streaming via SSE with green/red flash animations
- Simulated portfolio — $10k virtual cash, market orders, instant fills
- Portfolio visualizations — heatmap (treemap), P&L chart, positions table
- AI chat assistant — analyzes holdings, suggests and auto-executes trades
- Watchlist management — track tickers manually or via AI
- Dark terminal aesthetic — Bloomberg-inspired, data-dense layout

## Planned Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite with lazy initialization
- **AI**: LiteLLM → OpenRouter (Cerebras inference) with structured outputs
- **Market data**: Built-in GBM simulator (default) or Massive API (optional) — see [`planning/MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md)

## Running What Exists Today

```bash
cd backend
uv sync --extra dev
uv run pytest -v              # run the market data test suite
uv run market_data_demo.py    # live terminal dashboard of simulated prices
```

See [`backend/README.md`](backend/README.md) and [`backend/CLAUDE.md`](backend/CLAUDE.md) for details.

## Project Structure

```
finally/
├── backend/     # FastAPI uv project (market data subsystem complete; API/DB/LLM pending)
├── planning/    # Project specification and agent-facing docs
└── (frontend/, test/, scripts/, db/, Dockerfile — planned, not yet present)
```

## License

See [LICENSE](LICENSE).
