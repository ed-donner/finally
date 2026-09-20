"""FinAlly FastAPI application: routes, lifecycle and wiring of the five agents."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents import (
    AnalyzerAgent,
    OrchestratorAgent,
    PortfolioAgent,
    RiskAgent,
    WatchlistAgent,
)
from .agents.base import correlation_id, new_id
from .agents.models import (
    AnalysisReport,
    ChatRequest,
    ChatResponse,
    PortfolioSummary,
    RiskAssessment,
    Snapshot,
    TradeOrder,
    TradeRequest,
    TradeResult,
    WatchlistChange,
    WatchlistEntry,
    WatchlistResult,
    normalize_ticker,
)
from .db import PROJECT_ROOT, Database
from .market import MarketDataSource, PriceCache, create_market_data_source, create_stream_router
from .ratelimit import build_limiter, install_rate_limit

logger = logging.getLogger(__name__)

STATIC_DIR = Path(os.environ.get("FINALLY_STATIC_DIR") or PROJECT_ROOT / "static")
# Portfolio value is snapshotted this often for the P&L chart (PLAN.md section 7).
SNAPSHOT_INTERVAL_SECONDS = 30.0


# --------------------------------------------------------------------------- API models


class TradeResponse(BaseModel):
    # Returns the fresh portfolio too, so the UI can update without a second request.
    trade: TradeResult
    portfolio: PortfolioSummary


class WatchlistResponse(BaseModel):
    result: WatchlistResult
    watchlist: list[WatchlistEntry]


@dataclass
class Agents:
    """The wired agent bundle, stored on app.state.agents (typed, so typos are caught)."""

    db: Database
    source: MarketDataSource
    watchlist: WatchlistAgent
    portfolio: PortfolioAgent
    analyzer: AnalyzerAgent
    risk: RiskAgent
    orchestrator: OrchestratorAgent


# --------------------------------------------------------------------------- lifecycle


async def _snapshot_loop(portfolio: PortfolioAgent, interval: float) -> None:
    # Runs for the app lifetime; cancelled on shutdown.
    while True:
        await asyncio.sleep(interval)
        try:
            portfolio.record_snapshot()
        except Exception:  # noqa: BLE001 - a failed snapshot must not kill the loop
            logger.exception("portfolio snapshot failed")


def create_app(db_path: str | None = None, snapshot_interval: float = SNAPSHOT_INTERVAL_SECONDS):
    """Build the app. `db_path` (e.g. ":memory:") overrides FINALLY_DB_PATH, mainly for tests."""
    # Load .env before anything reads OPENROUTER_API_KEY / MASSIVE_API_KEY / LLM_MOCK.
    load_dotenv(PROJECT_ROOT / ".env")

    # Created here (not in lifespan) because the SSE router needs the cache at build time.
    cache = PriceCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(db_path)
        db.init_schema()  # lazy init: creates tables and seeds defaults if missing

        # Simulator by default; Massive if MASSIVE_API_KEY is set.
        source = create_market_data_source(cache)
        watchlist = WatchlistAgent(db, cache, source)
        portfolio = PortfolioAgent(db, cache)
        risk = RiskAgent(db, cache)
        analyzer = AnalyzerAgent(db, cache, risk.limits.warn_position_pct)
        orchestrator = OrchestratorAgent(db, portfolio, analyzer, risk, watchlist)

        # Price the watchlist plus any held positions from the start.
        await source.start(watchlist.tracked_tickers())
        portfolio.record_snapshot()  # gives the P&L chart a first point immediately
        task = asyncio.create_task(_snapshot_loop(portfolio, snapshot_interval))

        # Handlers reach the agents through app.state, so each app instance (e.g. per test)
        # has its own database and agents.
        app.state.agents = Agents(db, source, watchlist, portfolio, analyzer, risk, orchestrator)
        try:
            yield
        finally:
            # Shutdown: stop the snapshot loop, then the price source, then close the DB.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await source.stop()
            db.close()

    app = FastAPI(title="FinAlly", version="0.1.0", lifespan=lifespan)

    # Per-IP limits on POST /api/chat and the trade endpoints (env-configurable, in-memory).
    install_rate_limit(app, build_limiter())

    # Tag each request with a fresh correlation id so all agent_logs rows for it can be joined.
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        correlation_id.set(new_id())
        return await call_next(request)

    # Small accessor for the agent bundle created in lifespan.
    def agents(request: Request) -> Agents:
        return request.app.state.agents

    # ----------------------------------------------------------------- system

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # ----------------------------------------------------------------- market data (SSE)

    # The SSE endpoint (/api/stream/prices) comes from the market data package.
    app.include_router(create_stream_router(cache))

    # ----------------------------------------------------------------- portfolio

    @app.get("/api/portfolio", response_model=PortfolioSummary)
    def get_portfolio(request: Request):
        return agents(request).portfolio.get_summary()

    @app.get("/api/portfolio/history", response_model=list[Snapshot])
    def get_history(request: Request, limit: int = 1000):
        return agents(request).portfolio.get_history(max(1, min(limit, 5000)))

    # Two public paths for the same handler: /api/trade and the PLAN.md section 8 path.
    @app.post("/api/trade", response_model=TradeResponse)
    @app.post("/api/portfolio/trade", response_model=TradeResponse)
    async def trade(request: Request, body: TradeOrder):
        """Manual market order. Flow: price -> Risk -> Portfolio."""
        a = agents(request)
        # Provenance is server-owned: the client payload (TradeOrder) has no `source` field.
        req = TradeRequest(**body.model_dump(), source="manual")
        await a.watchlist.ensure_priced(req.ticker)
        verdict = a.risk.validate_trade(req, a.portfolio.get_summary())
        # Risk rejection -> 400 with every reason so the UI can show them.
        if not verdict.approved:
            raise HTTPException(
                status_code=400,
                detail={"error": "; ".join(verdict.reasons), "reasons": verdict.reasons},
            )
        result = a.portfolio.execute_trade(req, verdict)
        # Mechanical failure inside the transaction (e.g. a race): same shape of error.
        if not result.ok:
            raise HTTPException(
                status_code=400, detail={"error": result.error, "reasons": [result.error]}
            )
        return TradeResponse(trade=result, portfolio=a.portfolio.get_summary())

    # ----------------------------------------------------------------- analysis / risk

    @app.get("/api/analysis", response_model=AnalysisReport)
    def get_analysis(request: Request):
        a = agents(request)
        return a.analyzer.analyze(a.portfolio.get_summary(), a.watchlist.tickers())

    @app.get("/api/risk-assessment", response_model=RiskAssessment)
    def get_risk_assessment(request: Request):
        a = agents(request)
        return a.risk.assess(a.portfolio.get_summary())

    # ----------------------------------------------------------------- watchlist

    @app.get("/api/watchlist", response_model=list[WatchlistEntry])
    def get_watchlist(request: Request):
        return agents(request).watchlist.list_with_prices()

    def _watchlist_response(a: Agents, result: WatchlistResult) -> WatchlistResponse:
        return WatchlistResponse(result=result, watchlist=a.watchlist.list_with_prices())

    @app.post("/api/watchlist", response_model=WatchlistResponse)
    async def modify_watchlist(request: Request, body: WatchlistChange):
        """Add or remove a ticker: {"ticker": "PYPL", "action": "add" | "remove"}."""
        a = agents(request)
        # apply_changes takes a batch; here it is always exactly one change.
        (result,) = await a.watchlist.apply_changes([body], added_by="user")
        if not result.ok:
            raise HTTPException(status_code=400, detail={"error": result.error})
        return _watchlist_response(a, result)

    @app.delete("/api/watchlist/{ticker}", response_model=WatchlistResponse)
    async def delete_watchlist_ticker(request: Request, ticker: str):
        a = agents(request)
        result = await a.watchlist.remove(normalize_ticker(ticker))
        if not result.ok:
            raise HTTPException(status_code=404, detail={"error": result.error})
        return _watchlist_response(a, result)

    # ----------------------------------------------------------------- chat

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: Request, body: ChatRequest):
        return await agents(request).orchestrator.handle_message(body.message.strip())

    # ----------------------------------------------------------------- errors

    # Flatten errors to {"error": ...} (plus "reasons" for trades) instead of {"detail": ...}.
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content=detail)

    # ----------------------------------------------------------------- static frontend (last)

    # Mounted last so it never shadows /api routes; only present once the frontend is built.
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


# Module-level instance for `uvicorn app.main:app`.
app = create_app()
