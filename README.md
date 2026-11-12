# FinAlly - AI Trading Workstation

A Bloomberg-style trading terminal with live streaming prices, a simulated $10k portfolio, and an LLM chat assistant that can analyze positions and execute trades from natural language. Built by coding agents as a capstone for an agentic AI coding course.

## Status

- Done: market data (GBM simulator, Massive API client, shared price cache) in `backend/app/market/`. See `planning/MARKET_DATA_SUMMARY.md`.
- Planned: portfolio and watchlist API, SSE endpoint, frontend, chat, Docker, E2E tests. See `planning/PLAN.md` for the spec and build order.

## Architecture

Single container on port 8000:

- Frontend: Next.js (static export), TypeScript, Tailwind, Recharts
- Backend: FastAPI (Python, `uv`) with SSE price streaming
- Database: SQLite, created and seeded at startup
- AI: Anthropic Python SDK with structured outputs
- Market data: built-in simulator by default, Massive API if a key is set

## Configuration

Copy `.env.example` to `.env`:

| Variable | Description |
|---|---|
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` | LLM access; required for chat unless `LLM_MOCK=true` |
| `MASSIVE_API_KEY` | Optional; empty uses the simulator |
| `LLM_MOCK` | Default `false`; `true` gives deterministic mock chat replies |

## Run

Available now (market data only):

```bash
cd backend
uv sync --extra dev
uv run pytest
uv run market_data_demo.py
```

Planned, once the app is built:

```bash
scripts/start_mac.sh     # or scripts\start_windows.ps1
# flags: --build, --open (PowerShell: -Build, -Open)
# open http://localhost:8000
```

## Layout

```
backend/    FastAPI uv project (market data done)
planning/   Plan and agent docs
frontend/   Next.js project (planned)
test/       Playwright E2E tests (planned)
scripts/    Docker start/stop wrappers (planned)
```

## License

MIT. See [LICENSE](LICENSE).
