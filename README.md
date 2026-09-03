# FinAlly — AI Trading Workstation

A trading terminal that streams live market data, simulates portfolio trading, and puts
an LLM assistant beside your positions — one that can analyze holdings and execute trades
from natural language.

Built entirely by coding agents as the capstone for an agentic AI coding course. Agents
coordinate through shared docs in [`planning/`](planning/).

## Status

**Early — market data is the only component built.** There's no frontend and no Dockerfile
yet, so the app doesn't run end to end.

| Component | Status |
|---|---|
| Market data — simulator, Massive client, price cache, SSE | ✅ 73 tests, 84% coverage |
| Portfolio, trades, watchlist, chat API | ⬜ |
| Database, frontend, Docker, E2E tests | ⬜ |

## Try It

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+. No API key needed.

```bash
cd backend
uv sync --dev
uv run market_data_demo.py   # live terminal dashboard, 10 tickers, ~60s
uv run pytest                # test suite
```

## Architecture

Target design is one Docker container on port 8000: a **Next.js** static export served by
**FastAPI**, backed by **SQLite**, with **LiteLLM → OpenRouter** (Cerebras) for chat and
Server-Sent Events for price streaming. Single origin, so no CORS.

Market data has two interchangeable sources behind one interface — a GBM simulator
(default) and a Massive/Polygon.io poller (when `MASSIVE_API_KEY` is set) — both writing
to a thread-safe `PriceCache` that SSE, portfolio valuation, and trade pricing read from.
See [`planning/MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md).

## Configuration

Read from a gitignored `.env` in the project root.

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | LLM assistant. Everything except chat works without it. |
| `MASSIVE_API_KEY` | Optional — real market data. Omit to use the simulator. |
| `LLM_MOCK` | Optional — `true` for deterministic mock responses in tests. |

## Docs

- [`planning/PLAN.md`](planning/PLAN.md) — full spec, and the contract between agents
- [`backend/README.md`](backend/README.md) — backend development and testing

## License

See [LICENSE](LICENSE).
