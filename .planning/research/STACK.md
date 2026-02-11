# Stack Research

**Domain:** AI Trading Workstation (terminal UI + real-time data + LLM chat)
**Researched:** 2026-02-11
**Confidence:** HIGH

## Context

The market data subsystem is complete (FastAPI, Python/uv, GBM simulator, Massive API client, PriceCache, SSE streaming, 73 tests). This research covers everything else needed: database, portfolio APIs, LLM integration, frontend, and Docker packaging.

Existing backend dependencies: FastAPI >=0.115.0, uvicorn[standard] >=0.32.0, numpy >=2.0.0, massive >=1.0.0, rich >=13.0.0. Python >=3.12.

---

## Recommended Stack

### Backend — New Dependencies

| Technology | Version | Purpose | Why Recommended | Confidence |
|------------|---------|---------|-----------------|------------|
| aiosqlite | >=0.22.0 | Async SQLite access | Asyncio bridge to stdlib sqlite3. FastAPI is async-first; aiosqlite lets us `await` all DB calls without blocking the event loop. No ORM overhead — raw SQL keeps things simple and debuggable. v0.22.0+ changed Connection to no longer inherit from Thread; use as async context manager. | HIGH |
| litellm | >=1.78.0 | LLM API gateway | Unified OpenAI-compatible interface for 100+ providers. As of v1.77.5 (Sep 2025), natively supports `openrouter/openai/gpt-oss-120b`. Handles retries, cost tracking, and structured output passthrough. Pin to >=1.78.0 to ensure gpt-oss model support is stable. | HIGH |
| pydantic | >=2.10.0 | Structured output schemas + request/response validation | FastAPI already depends on Pydantic v2 internally. Using Pydantic models for LLM structured output schemas provides `.model_json_schema()` for generating JSON Schema and validates parsed responses. v2.12 is current but >=2.10 is safe. | HIGH |
| python-dotenv | >=1.0.0 | Load .env files | Reads OPENROUTER_API_KEY and MASSIVE_API_KEY from .env at project root. Standard for 12-factor apps. FastAPI does not auto-load .env; this is the canonical solution. Latest is 1.2.1. | HIGH |

### Frontend — Core

| Technology | Version | Purpose | Why Recommended | Confidence |
|------------|---------|---------|-----------------|------------|
| Next.js | 16.x (latest 16.1.5) | React framework with static export | `output: 'export'` produces a static SPA that FastAPI serves via `StaticFiles`. Next.js 16 uses Turbopack by default (5x faster builds), ships with React 19.2, and the App Router is stable. Static export means no Node server needed — perfect for single-container deployment. | HIGH |
| React | 19.x (19.2 via Next.js 16) | UI library | Ships as a peer dependency of Next.js 16. React 19 brings `use()`, Actions, and improved Suspense — none of which we rely on heavily, but we get them free. | HIGH |
| TypeScript | 5.x | Type safety | Next.js 16 includes TypeScript support out of the box. Catches bugs at build time, essential for complex state management in a trading UI. | HIGH |
| Tailwind CSS | 4.1.x (latest 4.1.18) | Utility-first CSS | v4 uses CSS-first configuration (no tailwind.config.js needed). 5x faster full builds, 100x faster incremental. Dark theme via CSS custom properties. Install: `tailwindcss` + `@tailwindcss/postcss` + `postcss`. Configure in `postcss.config.mjs` and import in CSS with `@import "tailwindcss"`. | HIGH |

### Frontend — Charting & Visualization

| Technology | Version | Purpose | Why Recommended | Confidence |
|------------|---------|---------|-----------------|------------|
| lightweight-charts | 5.1.0 | Main price chart + sparklines | TradingView's open-source HTML5 canvas charting library. Purpose-built for financial data. Renders 10,000+ data points smoothly. Has official React tutorial (useRef/useEffect pattern). Supports area, line, candlestick, histogram series. Canvas-based = performs far better than SVG for streaming data. | HIGH |
| Recharts | 3.7.0 | Treemap (portfolio heatmap) + P&L line chart | Declarative React charting built on D3. Has built-in `<Treemap>` component with custom content renderer — perfect for the portfolio heatmap colored by P&L. Also provides `<LineChart>` for portfolio value over time, `<ResponsiveContainer>` for auto-sizing. SVG-based but fine for these non-streaming charts. v3.7 is current, TypeScript-native, SSR-compatible. | HIGH |

### Frontend — Supporting Libraries

| Library | Version | Purpose | When to Use | Confidence |
|---------|---------|---------|-------------|------------|
| clsx | >=2.0 | Conditional CSS classes | Composing Tailwind classes conditionally (e.g., price flash green/red). Tiny, zero-dependency. | HIGH |
| tailwind-merge | >=2.0 | Merge Tailwind classes without conflicts | When component props override base classes. Prevents `text-red-500 text-green-500` conflicts. | MEDIUM |

### Development Tools

| Tool | Purpose | Notes | Confidence |
|------|---------|-------|------------|
| Node.js 20 LTS | Frontend build | Used in Docker Stage 1. LTS until Apr 2026. Next.js 16 requires Node >=18.18.0. | HIGH |
| Python 3.12 | Backend runtime | Already in use. Used in Docker Stage 2. | HIGH |
| uv | Python package manager | Already in use. Fast, reproducible lockfile. | HIGH |
| Playwright | E2E testing | Runs in separate test container. Supports SSE testing, screenshot comparison. | HIGH |
| pytest | Backend unit tests | Already in use with pytest-asyncio. | HIGH |

---

## Installation

### Backend (add to pyproject.toml dependencies)

```bash
cd backend
uv add aiosqlite
uv add litellm
uv add python-dotenv
# pydantic is already a transitive dependency of FastAPI — pin explicitly if needed:
uv add "pydantic>=2.10.0"
```

### Frontend (new project)

```bash
npx create-next-app@latest frontend --typescript --tailwind --app --no-src-dir --no-import-alias
cd frontend
npm install lightweight-charts recharts clsx
```

Note: `create-next-app` with `--tailwind` in Next.js 16 sets up Tailwind v4 automatically with the CSS-first config.

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|------------------------|
| lightweight-charts 5.x | Recharts for all charts | Never for streaming price data. Recharts is SVG/React re-render based — it cannot keep up with 500ms price ticks across 10+ tickers without jank. lightweight-charts uses impemental canvas updates via `series.update()`. |
| lightweight-charts 5.x | Apache ECharts | If you need 30+ chart types. Overkill for a trading terminal that needs line/area/candlestick. ECharts bundle is ~1MB vs ~45KB for lightweight-charts. |
| Recharts 3.x (for treemap) | D3.js directly | If you need full control over every pixel. D3 is imperative and verbose; Recharts wraps it declaratively. For a treemap + line chart, Recharts is 10x less code. |
| Recharts 3.x (for treemap) | Nivo | Similar capability but larger bundle and more opinionated styling. Recharts is lighter and more flexible for custom content (the P&L-colored treemap cells). |
| aiosqlite (raw SQL) | SQLAlchemy async | If schema complexity grows beyond 6 tables or you need migrations. For our 6-table schema with market-order-only trades, raw SQL is simpler, faster to write, and easier to debug. No ORM mapping overhead. |
| aiosqlite (raw SQL) | Tortoise ORM | If you want Django-style ORM in async Python. Adds complexity for little benefit at our scale. |
| litellm | Direct OpenRouter HTTP calls | If litellm adds unwanted overhead or breaks. OpenRouter's API is OpenAI-compatible; you could use `httpx` directly. LiteLLM's value is retry logic, cost tracking, and future provider flexibility. |
| Tailwind CSS v4 | CSS Modules | If you hate utility classes. But Tailwind is faster to iterate, produces smaller CSS, and the dark terminal theme maps naturally to utility classes. |
| Next.js static export | Vite + React | If you don't want/need Next.js conventions. Vite builds are fast and the output is simpler. But `create-next-app` gives us file-based routing, TypeScript config, and Tailwind setup in one command. For a course capstone, Next.js is the more marketable skill. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `lightweight-charts-react-wrapper` (npm) | Third-party wrapper with unclear maintenance. The official lightweight-charts docs provide a React tutorial using useRef/useEffect that is simpler, more current (supports v5), and doesn't add a dependency. | Direct integration following the [official React tutorial](https://tradingview.github.io/lightweight-charts/tutorials/react/simple) |
| Recharts for streaming price data | SVG-based with React re-renders on every data point. At 500ms ticks for 10 tickers = 20 renders/sec, this causes visible jank and GC pressure. | lightweight-charts (canvas, imperative `update()`) |
| WebSockets (e.g., socket.io) | Adds bidirectional complexity we don't need. Our data flow is one-way: server pushes prices. SSE is simpler, has built-in browser reconnection via EventSource, and works through proxies/CDNs. | SSE via EventSource (already implemented) |
| Tailwind CSS v3 | v4 is stable, faster, and uses CSS-first config. v3 requires a JS config file and is no longer the default in `create-next-app`. | Tailwind CSS v4 |
| SQLAlchemy | Too heavy for 6 tables with simple CRUD. Adds model definitions, session management, migration tooling (Alembic). Our schema is static and small. | aiosqlite with raw SQL |
| Prisma / Drizzle (TS ORMs) | Frontend doesn't touch the database. All DB access is through FastAPI REST endpoints. | N/A — backend owns all data access |
| shadcn/ui | Component library adds a layer of abstraction over Tailwind. For a Bloomberg-inspired terminal, we want full control over every visual detail. shadcn's opinionated spacing/rounding doesn't match the data-dense aesthetic. | Custom Tailwind components |
| Chart.js | Canvas-based but not designed for financial data. No built-in candlestick, no time-scale axis, no crosshair. Would need plugins for everything lightweight-charts does natively. | lightweight-charts |
| `next export` CLI command | Removed in Next.js 14+. Use `output: 'export'` in `next.config.ts` instead. | `output: 'export'` config |

---

## LLM Integration Details

### Model Path

```
Frontend → POST /api/chat → FastAPI → litellm.completion() → OpenRouter → Cerebras inference → gpt-oss-120b
```

### LiteLLM Configuration

```python
from litellm import completion

response = completion(
    model="openrouter/openai/gpt-oss-120b",
    messages=[...],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "chat_response",
            "strict": True,
            "schema": ChatResponse.model_json_schema()
        }
    },
    extra_body={
        "provider": {
            "require_parameters": True  # Force routing to providers that support json_schema
        }
    },
    api_key=os.getenv("OPENROUTER_API_KEY"),
    api_base="https://openrouter.ai/api/v1",
)
```

### Critical Notes

1. **LiteLLM + OpenRouter structured outputs**: As of late 2025, LiteLLM's `supports_response_schema()` may return `False` for OpenRouter models. The workaround is using `extra_body` to pass `provider.require_parameters: true` and passing the `response_format` directly. This was reported fixed in v1.77.5+ but should be validated during implementation. **Confidence: MEDIUM** — actively evolving integration.

2. **Pydantic schema generation**: Define the `ChatResponse` model in Pydantic, then use `ChatResponse.model_json_schema()` to generate the JSON Schema for `response_format`. This ensures the schema and validation are always in sync.

3. **Mock mode**: When `LLM_MOCK=true`, bypass litellm entirely and return deterministic responses. This is essential for E2E tests and development without an API key.

---

## Static File Serving Pattern

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI()

# API routes first (registered before static mount)
app.include_router(api_router, prefix="/api")
app.include_router(stream_router, prefix="/api/stream")

# Static files last — catch-all for frontend routes
app.mount("/", StaticFiles(directory=Path("static"), html=True), name="frontend")
```

The `html=True` flag enables serving `index.html` for directory paths, which is required for Next.js client-side routing to work.

---

## Version Compatibility Matrix

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| Next.js 16.x | React 19.2.x, React DOM 19.2.x | Peer dependencies; install together |
| Next.js 16.x | Tailwind CSS 4.x | `create-next-app --tailwind` installs v4 by default |
| Tailwind CSS 4.x | @tailwindcss/postcss 4.x, postcss 8.x | Required PostCSS plugin; replaces old `tailwindcss` PostCSS plugin |
| lightweight-charts 5.x | Any React (used via refs, not JSX) | No React peer dependency; integrates via useRef/useEffect |
| Recharts 3.x | React >=18 | Supports React 19 (tested with 3.7.0) |
| FastAPI >=0.115 | Pydantic >=2.7 | FastAPI vendors Pydantic; ensure compatible versions |
| litellm >=1.78 | pydantic >=2.0, httpx | Uses Pydantic internally for response models |
| aiosqlite >=0.22 | Python >=3.9 | We use Python 3.12; fully compatible |

---

## Docker Build Implications

```dockerfile
# Stage 1: Build frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
# Output: /app/frontend/out/ (Next.js static export)

# Stage 2: Python backend
FROM python:3.12-slim AS runtime
# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/ ./
RUN uv sync --frozen
# Copy frontend build output
COPY --from=frontend-build /app/frontend/out/ ./static/
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Key points:
- Node 20 slim for frontend build (smallest image with full npm support)
- Python 3.12 slim for runtime
- `uv sync --frozen` uses the lockfile for reproducible installs
- Frontend static output goes to `static/` directory in the backend

---

## Sources

- [Next.js v16.1.5 docs (Context7: /vercel/next.js/v16.1.5)](https://github.com/vercel/next.js) — static export configuration, verified HIGH
- [Lightweight Charts (Context7: /tradingview/lightweight-charts)](https://tradingview.github.io/lightweight-charts/) — React integration, series API, verified HIGH
- [LiteLLM docs (Context7: /websites/litellm_ai)](https://docs.litellm.ai/) — structured outputs, OpenRouter provider, json_schema mode, verified HIGH
- [Tailwind CSS v4 (Context7: /websites/tailwindcss)](https://tailwindcss.com/) — installation, CSS-first config, PostCSS setup, verified HIGH
- [Recharts v3.3 (Context7: /recharts/recharts/v3.3.0)](https://recharts.org/) — Treemap component, custom content, verified HIGH
- [Pydantic v2 (Context7: /pydantic/pydantic)](https://docs.pydantic.dev/) — model_json_schema, validated HIGH
- [aiosqlite on PyPI](https://pypi.org/project/aiosqlite/) — v0.22.1 current, verified HIGH
- [litellm on PyPI](https://pypi.org/project/litellm/) — v1.81.9 current, verified HIGH
- [LiteLLM gpt-oss support (GitHub issue #13428)](https://github.com/BerriAI/litellm/issues/13428) — merged in v1.77.5, verified MEDIUM
- [OpenRouter structured outputs docs](https://openrouter.ai/docs/guides/features/structured-outputs) — json_schema + require_parameters, verified MEDIUM
- [LiteLLM + OpenRouter structured outputs workaround (GitHub discussion #11652)](https://github.com/BerriAI/litellm/discussions/11652) — extra_body approach, verified MEDIUM
- [Next.js 16 blog post](https://nextjs.org/blog/next-16) — React 19.2, Turbopack default, verified HIGH
- [Tailwind CSS npm](https://www.npmjs.com/package/tailwindcss) — v4.1.18 current, verified HIGH
- [lightweight-charts npm](https://www.npmjs.com/package/lightweight-charts) — v5.1.0 current, verified HIGH
- [Recharts npm](https://www.npmjs.com/package/recharts) — v3.7.0 current, verified HIGH
- [FastAPI static files docs](https://fastapi.tiangolo.com/tutorial/static-files/) — StaticFiles mount with html=True, verified HIGH
- [python-dotenv on PyPI](https://pypi.org/project/python-dotenv/) — v1.2.1 current, verified HIGH

---
*Stack research for: FinAlly AI Trading Workstation*
*Researched: 2026-02-11*
