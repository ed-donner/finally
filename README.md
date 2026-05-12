# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation built as a capstone project for an agentic AI coding course. Streams live market data, supports simulated portfolio trading, and includes an LLM chat assistant that can analyze positions and execute trades via natural language.

**Built entirely by AI coding agents.**

---

## What It Does

- **Live price streaming** — prices flash green/red on tick via SSE, with sparkline mini-charts
- **Simulated trading** — $10,000 virtual cash, instant market-order fills, no fees
- **Portfolio visualization** — treemap heatmap sized by weight and colored by P&L, plus a P&L history chart
- **AI chat assistant** — ask about your portfolio, get analysis, have the AI execute trades and manage your watchlist

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js (TypeScript, static export) |
| Backend | FastAPI + Python (managed via `uv`) |
| Database | SQLite (lazy-initialized, volume-mounted) |
| Real-time | Server-Sent Events (SSE) |
| AI | LiteLLM → OpenRouter (Cerebras inference) |
| Market data | Built-in GBM simulator (or Massive API with key) |
| Deployment | Single Docker container, port 8000 |

## Quick Start

> Requires Docker and an OpenRouter API key.

```bash
# 1. Copy and fill in env vars
cp .env.example .env
# Add your OPENROUTER_API_KEY to .env

# 2. Start the app
./scripts/start_mac.sh        # macOS/Linux
.\scripts\start_windows.ps1   # Windows PowerShell

# 3. Open http://localhost:8000
```

To stop: `./scripts/stop_mac.sh`

## Environment Variables

```bash
# Required
OPENROUTER_API_KEY=your-key-here

# Optional — omit to use the built-in market simulator
MASSIVE_API_KEY=

# Optional — set to "true" for deterministic mock LLM responses (E2E tests)
LLM_MOCK=false
```

## Project Structure

```
finally/
├── frontend/          # Next.js TypeScript app (static export)
├── backend/           # FastAPI uv project
│   └── db/            # Schema SQL and seed logic
├── planning/          # Agent documentation and project spec
│   └── PLAN.md        # Full project specification
├── scripts/           # Start/stop Docker scripts
├── test/              # Playwright E2E tests
├── db/                # Runtime volume mount (SQLite file written here)
└── Dockerfile         # Multi-stage build (Node → Python)
```

## For Contributors / Agents

All project documentation lives in `planning/`. Start with [`planning/PLAN.md`](planning/PLAN.md) — it is the authoritative spec for architecture, API contracts, data schema, and design decisions.

Key boundaries:
- `frontend/` and `backend/` are self-contained projects that communicate only via `/api/*` and `/api/stream/*`
- The backend owns all database logic, SSE streaming, market data, and LLM integration
- The frontend is a pure static export — no SSR, no server components
