# FinAlly — AI Trading Workstation

FinAlly (Finance Ally) is a Bloomberg-style trading terminal with an AI copilot. It streams live prices, lets you trade a simulated $10,000 portfolio, and includes an LLM chat assistant that can analyze positions and execute trades or manage your watchlist through natural language.

Built entirely by coding agents as the capstone of an agentic AI coding course.

## Status

| Component | State |
| --- | --- |
| Market data (simulator + Massive client, price cache) | Done — `backend/app/market/` |
| Database, trading logic, REST/SSE API, LLM chat | Planned |
| Next.js frontend | Planned |
| Docker packaging, start scripts, E2E tests | Planned |

Full spec: [`planning/PLAN.md`](planning/PLAN.md). Market data design: [`planning/MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md).

## Features (target)

- Live watchlist with price flashes and sparklines
- Market-order trading, fractional shares, no fees
- Portfolio heatmap, positions table, P&L chart
- AI chat that auto-executes trades and watchlist changes
- Single container, single port, no login

## Architecture

- **Backend:** FastAPI (Python, `uv`), SQLite, Server-Sent Events
- **Frontend:** Next.js (TypeScript), static export served by FastAPI
- **AI:** LiteLLM → OpenRouter (Cerebras) with structured outputs
- **Market data:** built-in simulator by default; real data via the Massive API if a key is set

## Quick start (once complete)

```bash
cp .env.example .env     # add OPENROUTER_API_KEY for chat
docker compose up -d     # open http://localhost:8000
docker compose down      # stop; data persists (`down -v` resets it)
```

The app starts without a `.env`; only chat is disabled.

## Configuration

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | Enables AI chat |
| `MASSIVE_API_KEY` | Optional. Real market data instead of the simulator |
| `LLM_MOCK` | `true` for deterministic mock LLM replies (testing) |
| `DB_PATH` | SQLite file location |

On the Massive free tier (5 calls/min) prices update every 15 seconds, so the UI looks fairly static. The simulator is recommended for most users.

## Development

```bash
cd backend
uv sync
uv run market_data_demo.py   # market data demo
uv run pytest
```

## License

See [`LICENSE`](LICENSE).
