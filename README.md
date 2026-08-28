# FinAlly — AI Trading Workstation

A visually stunning AI-powered trading workstation that streams live market data, simulates portfolio trading, and integrates an LLM chat assistant that can analyze positions and execute trades via natural language.

Built entirely by coding agents as a capstone project for an agentic AI coding course.

## Features

- **Live price streaming** via SSE with green/red flash animations
- **Simulated portfolio** — $10k virtual cash, market orders, instant fills
- **Portfolio visualizations** — heatmap (treemap), P&L chart, positions table
- **AI chat assistant** — analyzes holdings, suggests and auto-executes trades; runs on a free model, so the whole app costs nothing to operate
- **Watchlist management** — track tickers manually or via AI
- **Dark terminal aesthetic** — Bloomberg-inspired, data-dense layout

## Architecture

Single Docker container serving everything on port 8000:

- **Frontend**: Next.js (static export) with TypeScript and Tailwind CSS
- **Backend**: FastAPI (Python/uv) with SSE streaming
- **Database**: SQLite with lazy initialization
- **AI**: LiteLLM → OpenRouter with a free model, using forced tool calling
- **Market data**: Built-in GBM simulator (default) or Massive API (optional)

## Quick Start

```bash
# Clone and configure
cp .env.example .env
# Add your OPENROUTER_API_KEY to .env

# Run with Docker
docker build -t finally .
docker run -v "$PWD/db:/app/db" -p 8000:8000 --env-file .env finally

# Open http://localhost:8000
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key for AI chat |
| `OPENROUTER_MODEL` | No | Model to use; defaults to `openrouter/nvidia/nemotron-3.5-lightning:free` |
| `MASSIVE_API_KEY` | No | Massive (Polygon.io) key for real market data; omit to use simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing) |

Write variable names in uppercase. Docker passes `.env` through verbatim, and the Linux
container is case-sensitive even though Windows is not.

## Notes on the free model

The AI chat runs on a free OpenRouter model, so the app costs nothing to operate. Two things
follow from that, both by design rather than by accident:

- **Responses take 7-58 seconds**, typically 20-30. The chat panel shows an elapsed counter
  and says so plainly instead of hiding the wait behind a spinner.
- **Free models allow 20 requests per minute and 50 per day** (1000 per day once 10 USD of
  credit has been bought on the account). Run tests with `LLM_MOCK=true` rather than
  spending that budget.

Setting `OPENROUTER_MODEL` to a paid model works and is much faster, but is not required.

## Troubleshooting

**Certificate errors during setup** (`CERTIFICATE_VERIFY_FAILED`, `invalid peer certificate`)
mean your network inspects TLS traffic, common on corporate networks. Use `uv --system-certs`
and add `truststore` so Python trusts the certificates in your OS store.

## Project Structure

```
finally/
├── frontend/    # Next.js static export
├── backend/     # FastAPI uv project
├── planning/    # Project documentation and agent contracts
├── test/        # Playwright E2E tests
├── db/          # SQLite volume mount (runtime)
└── scripts/     # Start/stop helpers
```

## License

See [LICENSE](LICENSE).
