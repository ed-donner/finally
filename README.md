# FinAlly — AI Trading Workstation

An AI-powered trading workstation that streams live market data, simulates a $10k portfolio, and ships with an LLM chat assistant that can analyze positions and execute approved trades via natural language.

Built as the capstone project for an agentic AI coding course. The full specification lives in [`planning/PLAN.md`](planning/PLAN.md).

## Stack

- **Backend** — FastAPI (Python 3.12+, managed with `uv`), SQLite, SSE streaming
- **Frontend** — Next.js 15 + React 19, TypeScript, Tailwind CSS, TradingView Lightweight Charts, Recharts
- **AI** — LiteLLM → OpenRouter (`openrouter/openai/gpt-oss-120b`) with structured outputs
- **Market data** — In-process GBM simulator by default, optional Massive REST client

## Running Locally

Configure environment variables at the repo root in `.env` (all optional):

```bash
OPENROUTER_API_KEY=     # omit to run chat in deterministic mock mode
MASSIVE_API_KEY=        # omit to run the built-in simulator
LLM_MOCK=false          # true forces mock chat responses
SIMULATOR_SEED=         # integer seed for reproducible price paths
```

Backend (serves the API on http://localhost:8000):

```bash
cd backend
uv sync --extra dev
uv run uvicorn app.main:app --reload
```

Frontend (Next.js dev server on http://localhost:3000, proxies to the backend):

```bash
cd frontend
npm install
npm run dev
```

The backend lazily creates `backend/db/finally.db` on first request and seeds the default watchlist (AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX), a `default` user profile with $10,000 cash, and an initial portfolio snapshot.

## Tests

```bash
cd backend
uv run --extra dev pytest           # full suite
uv run --extra dev ruff check .     # lint
```

## Repository Layout

```
finally/
├── backend/    FastAPI app (app/), schema + seed (db/), pytest suite (tests/)
├── frontend/   Next.js app (app/, components/, lib/)
├── planning/   Project specification — PLAN.md is the source of truth
└── CLAUDE.md   Agent instructions
```

Docker packaging, start/stop scripts, and end-to-end Playwright tests described in `planning/PLAN.md` are not yet in the repo.

## License

See [LICENSE](LICENSE).
