# FinAlly — AI Trading Workstation

An AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM assistant that can analyse positions and execute trades through natural language.

Built by coding agents as the capstone project for an agentic AI coding course. The full specification is in [planning/PLAN.md](planning/PLAN.md), which agents use as their shared contract.

## Status

Early development. Only the market data subsystem is built.

| Component | State |
| --- | --- |
| Market data — simulator, Massive API client, price cache, SSE endpoint | Built, 73 tests passing |
| Database, portfolio, trading | Not started |
| LLM chat assistant | Not started |
| Frontend | Not started |
| Docker packaging | Not started |

There is no runnable application yet — no Dockerfile, no frontend, no API server. The sections below describe what exists today.

## Running what exists

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cd backend
uv sync

# Live terminal dashboard: 10 tickers with sparklines and colour-coded moves.
# Runs 60 seconds, or until Ctrl+C. No API key needed.
uv run market_data_demo.py

# Test suite
uv run pytest
```

## Market data

Two interchangeable sources sit behind one abstract interface (`MarketDataSource`):

- **Simulator** (default) — geometric Brownian motion with per-ticker drift and volatility, sector-correlated moves, and occasional random shocks. Runs in-process with no external dependencies.
- **Massive API** (optional) — REST polling against Polygon.io. Selected automatically when `MASSIVE_API_KEY` is set.

Both write to a thread-safe `PriceCache`. Everything downstream — the SSE endpoint, and later portfolio valuation and trade execution — reads from that cache and never touches the source directly, so the rest of the system does not care which one is running.

Module-level detail is in [planning/MARKET_DATA_SUMMARY.md](planning/MARKET_DATA_SUMMARY.md).

## Environment variables

Create a `.env` file in the project root:

| Variable | Required | Description |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Later | OpenRouter key for the AI chat assistant. Not used yet. |
| `MASSIVE_API_KEY` | No | Polygon.io key for real market data. Omit to use the simulator. |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses in tests. |

## Planned architecture

A single Docker container serving everything on port 8000:

- **Frontend** — a static build served by FastAPI, so there is one origin and no CORS setup
- **Backend** — FastAPI managed with uv, pushing live prices over SSE
- **Database** — SQLite, a single volume-mounted file
- **AI** — LiteLLM to OpenRouter, using structured outputs to drive trade execution

## Project structure

```
finally/
├── backend/              FastAPI uv project
│   ├── app/market/       Market data subsystem (built)
│   └── tests/            Unit and integration tests
└── planning/             Specification and agent contracts
    ├── PLAN.md
    └── MARKET_DATA_SUMMARY.md
```

## License

See [LICENSE](LICENSE).
