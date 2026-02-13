"""FinAlly FastAPI application."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import (
    CUSTOM_GROUP_KEY,
    CUSTOM_GROUP_LABEL,
    CUSTOM_GROUP_ORDER,
    DEFAULT_USER_ID,
    encode_actions,
    ensure_db_initialized,
    get_connection,
    get_db_path,
    now_iso,
)
from .llm import generate_chat_response
from .market import PriceCache, create_market_data_source, create_stream_router
from .market.seed_prices import SEED_PRICES

logger = logging.getLogger(__name__)


class TradeRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)
    quantity: float = Field(gt=0)
    side: Literal["buy", "sell"]


class WatchlistRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class AppState:
    def __init__(self) -> None:
        self.price_cache = PriceCache()
        self.market_source = None
        self.snapshot_task: asyncio.Task | None = None


def _ticker(value: str) -> str:
    ticker = value.upper().strip()
    if not ticker.isalnum():
        raise HTTPException(status_code=400, detail="Ticker must be alphanumeric")
    return ticker


def _fetch_watchlist_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY group_order, item_order, ticker",
        (DEFAULT_USER_ID,),
    ).fetchall()
    return [row["ticker"] for row in rows]


def _next_custom_item_order(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(item_order), -1) + 1 AS next_order FROM watchlist WHERE user_id = ? AND group_key = ?",
        (DEFAULT_USER_ID, CUSTOM_GROUP_KEY),
    ).fetchone()
    return int(row["next_order"])


def _insert_watchlist_ticker(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute(
        "INSERT INTO watchlist (id, user_id, ticker, group_key, group_label, group_order, item_order, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid4()),
            DEFAULT_USER_ID,
            ticker,
            CUSTOM_GROUP_KEY,
            CUSTOM_GROUP_LABEL,
            CUSTOM_GROUP_ORDER,
            _next_custom_item_order(conn),
            now_iso(),
        ),
    )


def _get_profile(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, cash_balance FROM users_profile WHERE id = ?",
        (DEFAULT_USER_ID,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="User profile missing")
    return row


def _list_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = ? ORDER BY ticker",
        (DEFAULT_USER_ID,),
    ).fetchall()


def _build_portfolio(price_cache: PriceCache, conn: sqlite3.Connection) -> dict:
    profile = _get_profile(conn)
    cash_balance = float(profile["cash_balance"])
    positions = _list_positions(conn)

    unrealized_total = 0.0
    invested_total = 0.0
    position_items = []

    for pos in positions:
        ticker = pos["ticker"]
        quantity = float(pos["quantity"])
        avg_cost = float(pos["avg_cost"])
        current_price = price_cache.get_price(ticker) or SEED_PRICES.get(ticker, avg_cost)
        market_value = quantity * current_price
        cost_value = quantity * avg_cost
        unrealized_pnl = market_value - cost_value
        unrealized_percent = (unrealized_pnl / cost_value * 100.0) if cost_value else 0.0

        unrealized_total += unrealized_pnl
        invested_total += market_value
        position_items.append(
            {
                "ticker": ticker,
                "quantity": round(quantity, 8),
                "avg_cost": round(avg_cost, 4),
                "current_price": round(current_price, 4),
                "market_value": round(market_value, 4),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "unrealized_pnl_percent": round(unrealized_percent, 4),
            }
        )

    total_value = cash_balance + invested_total
    return {
        "cash_balance": round(cash_balance, 4),
        "total_value": round(total_value, 4),
        "unrealized_pnl": round(unrealized_total, 4),
        "positions": position_items,
    }


def _record_snapshot(conn: sqlite3.Connection, price_cache: PriceCache) -> dict:
    portfolio = _build_portfolio(price_cache, conn)
    now = now_iso()
    conn.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, ?, ?, ?)",
        (str(uuid4()), DEFAULT_USER_ID, portfolio["total_value"], now),
    )
    return {"total_value": portfolio["total_value"], "recorded_at": now}


def _execute_trade(conn: sqlite3.Connection, price_cache: PriceCache, req: TradeRequest) -> dict:
    ticker = _ticker(req.ticker)
    quantity = float(req.quantity)
    side = req.side

    price = price_cache.get_price(ticker)
    if price is None:
        price = SEED_PRICES.get(ticker, 100.0)
        price_cache.update(ticker=ticker, price=price)

    profile = _get_profile(conn)
    cash_balance = float(profile["cash_balance"])
    notional = price * quantity

    current_position = conn.execute(
        "SELECT id, quantity, avg_cost FROM positions WHERE user_id = ? AND ticker = ?",
        (DEFAULT_USER_ID, ticker),
    ).fetchone()

    if side == "buy":
        if cash_balance < notional:
            raise HTTPException(status_code=400, detail="Insufficient cash balance")

        new_cash = cash_balance - notional
        conn.execute(
            "UPDATE users_profile SET cash_balance = ? WHERE id = ?",
            (new_cash, DEFAULT_USER_ID),
        )

        if current_position is None:
            conn.execute(
                "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid4()), DEFAULT_USER_ID, ticker, quantity, price, now_iso()),
            )
        else:
            old_qty = float(current_position["quantity"])
            old_avg = float(current_position["avg_cost"])
            new_qty = old_qty + quantity
            new_avg = ((old_qty * old_avg) + notional) / new_qty
            conn.execute(
                "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? WHERE id = ?",
                (new_qty, new_avg, now_iso(), current_position["id"]),
            )

    else:
        if current_position is None:
            raise HTTPException(status_code=400, detail="No position to sell")

        old_qty = float(current_position["quantity"])
        if quantity > old_qty:
            raise HTTPException(status_code=400, detail="Insufficient shares")

        new_cash = cash_balance + notional
        conn.execute(
            "UPDATE users_profile SET cash_balance = ? WHERE id = ?",
            (new_cash, DEFAULT_USER_ID),
        )

        new_qty = old_qty - quantity
        if new_qty <= 1e-12:
            conn.execute("DELETE FROM positions WHERE id = ?", (current_position["id"],))
        else:
            conn.execute(
                "UPDATE positions SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now_iso(), current_position["id"]),
            )

    conn.execute(
        "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid4()), DEFAULT_USER_ID, ticker, side, quantity, price, now_iso()),
    )

    snapshot = _record_snapshot(conn, price_cache)
    return {
        "ticker": ticker,
        "side": side,
        "quantity": round(quantity, 8),
        "price": round(price, 4),
        "notional": round(notional, 4),
        "snapshot": snapshot,
    }


async def _snapshot_loop(state: AppState, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            with get_connection() as conn:
                _record_snapshot(conn, state.price_cache)
                conn.commit()
        except Exception:
            logger.exception("Failed to record portfolio snapshot")


def _static_dir() -> Path | None:
    configured = os.environ.get("FINALLY_STATIC_DIR")
    root = Path(__file__).resolve().parents[2]
    candidates = [
        Path(configured) if configured else None,
        root / "frontend" / "out",
        root / "frontend" / "dist",
        root / "static",
    ]
    for path in candidates:
        if path and path.exists() and path.is_dir():
            return path
    return None


def create_app() -> FastAPI:
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_db_initialized()

        with get_connection() as conn:
            watchlist = _fetch_watchlist_tickers(conn)

        state.market_source = create_market_data_source(state.price_cache)
        await state.market_source.start(watchlist)

        interval = float(os.environ.get("SNAPSHOT_INTERVAL_SECONDS", "30"))
        state.snapshot_task = asyncio.create_task(_snapshot_loop(state, interval), name="portfolio-snapshot")

        try:
            yield
        finally:
            if state.snapshot_task:
                state.snapshot_task.cancel()
                try:
                    await state.snapshot_task
                except asyncio.CancelledError:
                    pass
            if state.market_source:
                await state.market_source.stop()

    app = FastAPI(title="FinAlly Backend", version="0.1.0", lifespan=lifespan)
    app.include_router(create_stream_router(state.price_cache))

    @app.get("/api/health")
    async def health() -> dict:
        ensure_db_initialized()
        source_name = "unknown"
        if state.market_source is not None:
            source_name = state.market_source.__class__.__name__
        return {
            "status": "ok",
            "db_path": str(get_db_path()),
            "market_source": source_name,
        }

    @app.get("/api/watchlist")
    async def get_watchlist() -> dict:
        ensure_db_initialized()
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker, group_key, group_label, group_order, item_order, added_at "
                "FROM watchlist WHERE user_id = ? ORDER BY group_order, item_order, ticker",
                (DEFAULT_USER_ID,),
            ).fetchall()

        items = []
        for row in rows:
            ticker = row["ticker"]
            update = state.price_cache.get(ticker)
            fallback_price = SEED_PRICES.get(ticker, 100.0)
            items.append(
                {
                    "ticker": ticker,
                    "added_at": row["added_at"],
                    "price": update.price if update else fallback_price,
                    "previous_price": update.previous_price if update else fallback_price,
                    "day_baseline_price": update.day_baseline_price if update else fallback_price,
                    "direction": update.day_direction if update else "flat",
                    "change_percent": update.day_change_percent if update else 0.0,
                    "intraday_change_percent": update.change_percent if update else 0.0,
                    "group": {
                        "key": row["group_key"] or CUSTOM_GROUP_KEY,
                        "label": row["group_label"] or CUSTOM_GROUP_LABEL,
                        "order": row["group_order"] if row["group_order"] is not None else CUSTOM_GROUP_ORDER,
                        "item_order": row["item_order"] if row["item_order"] is not None else 0,
                    },
                }
            )

        return {"items": items}

    @app.post("/api/watchlist", status_code=201)
    async def add_watchlist(request: WatchlistRequest) -> dict:
        ensure_db_initialized()
        ticker = _ticker(request.ticker)
        with get_connection() as conn:
            exists = conn.execute(
                "SELECT id FROM watchlist WHERE user_id = ? AND ticker = ?",
                (DEFAULT_USER_ID, ticker),
            ).fetchone()
            if exists:
                raise HTTPException(status_code=409, detail="Ticker already in watchlist")

            _insert_watchlist_ticker(conn, ticker)
            conn.commit()

        if state.market_source:
            await state.market_source.add_ticker(ticker)

        return {"ticker": ticker}

    @app.delete("/api/watchlist/{ticker}", status_code=204)
    async def delete_watchlist(ticker: str) -> Response:
        ensure_db_initialized()
        clean = _ticker(ticker)

        with get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
                (DEFAULT_USER_ID, clean),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Ticker not in watchlist")

        if state.market_source:
            await state.market_source.remove_ticker(clean)
        return Response(status_code=204)

    @app.get("/api/portfolio")
    async def get_portfolio() -> dict:
        ensure_db_initialized()
        with get_connection() as conn:
            return _build_portfolio(state.price_cache, conn)

    @app.post("/api/portfolio/trade")
    async def post_trade(request: TradeRequest) -> dict:
        ensure_db_initialized()
        with get_connection() as conn:
            executed = _execute_trade(conn, state.price_cache, request)
            portfolio = _build_portfolio(state.price_cache, conn)
            conn.commit()

        return {"trade": executed, "portfolio": portfolio}

    @app.get("/api/portfolio/history")
    async def get_portfolio_history(limit: int = 200) -> dict:
        ensure_db_initialized()
        if limit < 1 or limit > 2000:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 2000")

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT total_value, recorded_at FROM portfolio_snapshots WHERE user_id = ? "
                "ORDER BY recorded_at DESC LIMIT ?",
                (DEFAULT_USER_ID, limit),
            ).fetchall()

        return {
            "items": [
                {"total_value": float(row["total_value"]), "recorded_at": row["recorded_at"]}
                for row in reversed(rows)
            ]
        }

    @app.post("/api/chat")
    async def post_chat(request: ChatRequest) -> dict:
        ensure_db_initialized()

        with get_connection() as conn:
            conn.execute(
                "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid4()), DEFAULT_USER_ID, "user", request.message, None, now_iso()),
            )

            portfolio = _build_portfolio(state.price_cache, conn)
            watchlist = _fetch_watchlist_tickers(conn)
            history_rows = conn.execute(
                "SELECT role, content FROM chat_messages WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
                (DEFAULT_USER_ID,),
            ).fetchall()
            history = [{"role": row["role"], "content": row["content"]} for row in reversed(history_rows)]
            conn.commit()

        context = {
            "portfolio": portfolio,
            "watchlist": [
                {
                    "ticker": ticker,
                    "price": state.price_cache.get_price(ticker) or SEED_PRICES.get(ticker),
                }
                for ticker in watchlist
            ],
        }

        llm_response = await generate_chat_response(
            user_message=request.message,
            history=history,
            context=context,
        )

        executed_trades = []
        executed_watchlist_changes = []
        errors: list[str] = []

        with get_connection() as conn:
            for trade in llm_response.trades:
                try:
                    executed_trades.append(
                        _execute_trade(
                            conn,
                            state.price_cache,
                            TradeRequest(
                                ticker=trade["ticker"],
                                side=trade["side"],
                                quantity=trade["quantity"],
                            ),
                        )
                    )
                except HTTPException as exc:
                    errors.append(f"trade {trade['side']} {trade['ticker']}: {exc.detail}")

            for change in llm_response.watchlist_changes:
                ticker = _ticker(change["ticker"])
                action = change["action"]
                try:
                    if action == "add":
                        _insert_watchlist_ticker(conn, ticker)
                        if state.market_source:
                            await state.market_source.add_ticker(ticker)
                    else:
                        cursor = conn.execute(
                            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
                            (DEFAULT_USER_ID, ticker),
                        )
                        if cursor.rowcount == 0:
                            raise HTTPException(status_code=404, detail="Ticker not in watchlist")
                        if state.market_source:
                            await state.market_source.remove_ticker(ticker)

                    executed_watchlist_changes.append({"ticker": ticker, "action": action})
                except sqlite3.IntegrityError:
                    errors.append(f"watchlist {action} {ticker}: already exists")
                except HTTPException as exc:
                    errors.append(f"watchlist {action} {ticker}: {exc.detail}")

            actions_payload = {
                "trades": executed_trades,
                "watchlist_changes": executed_watchlist_changes,
                "errors": errors,
            }
            conn.execute(
                "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    DEFAULT_USER_ID,
                    "assistant",
                    llm_response.message,
                    encode_actions(actions_payload),
                    now_iso(),
                ),
            )
            conn.commit()

        return {
            "message": llm_response.message,
            "actions": {
                "trades": executed_trades,
                "watchlist_changes": executed_watchlist_changes,
                "errors": errors,
            },
        }

    static_dir = _static_dir()
    if static_dir:
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:

        @app.get("/")
        async def root() -> dict:
            return {"name": "FinAlly backend", "status": "ready"}

    return app


app = create_app()
