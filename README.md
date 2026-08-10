# FinAlly — AI-Powered Trading Workstation

**FinAlly** (Finance Ally) is an AI-powered trading workstation featuring real-time market data streaming via Server-Sent Events (SSE), simulated portfolio management, and an integrated LLM trading copilot.

## Features

- ⚡ **Live Market Streaming**: Real-time tick updates via SSE, powered by an internal Geometric Brownian Motion (GBM) simulator or Polygon.io (Massive API).
- 💼 **Simulated Trading**: Virtual portfolio management with real-time unrealized P&L tracking and instant market order execution.
- 🤖 **AI Trading Copilot**: LLM-driven assistant for natural-language portfolio analysis, trend monitoring, and automated order execution.
- 📊 **Terminal UI**: Dark-mode Bloomberg-inspired workstation with dynamic price flash animations and sparklines.

## Tech Stack

- **Backend**: Python 3.12+, FastAPI, SQLite, SSE ([`uv`](https://github.com/astral-sh/uv))
- **Frontend**: Next.js, TypeScript
- **Integrations**: OpenRouter / LiteLLM, Polygon.io (Massive API)

## Quick Start

### Backend Setup & Demo

```bash
cd backend

# Sync dependencies & run test suite
uv sync --dev
uv run pytest

# Run live market streaming engine demo
uv run python market_data_demo.py
```

### Environment Setup (Optional)

```bash
export MASSIVE_API_KEY="your_polygon_api_key"    # Live market data (defaults to internal GBM simulator)
export OPENROUTER_API_KEY="your_openrouter_key"  # LLM copilot integration
```

## Project Structure

- [`backend/`](backend) — FastAPI server, SSE market engine & SQLite database
- [`frontend/`](frontend) — Next.js trading workstation interface
- [`planning/`](planning) — Architectural specifications & design documentation

## Key Documentation

- [`PLAN.md`](planning/PLAN.md) — Product specification & architecture overview
- [`MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md) — Market data subsystem design summary

## License

[MIT](LICENSE)

