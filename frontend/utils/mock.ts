// Mock backend: an in-memory stand-in for the FastAPI API and the SSE price stream, so the UI
// runs (and can be developed) without a server. Behaviour mirrors the real backend's rules:
// market orders at the current price, weighted-average cost, cash/shares validation, and a
// 50% max position weight. Enable with ?mock=1 or NEXT_PUBLIC_USE_MOCK=true (see utils/api.ts).

import { ApiError } from "./errors";
import type {
  AnalysisReport,
  ChatResponse,
  Portfolio,
  Position,
  PriceTick,
  RiskAssessment,
  Snapshot,
  TradeOrder,
  TradeResponse,
  TradeResult,
  WatchlistEntry,
  WatchlistResponse,
  WatchlistResult,
} from "./types";

const START_CASH = 10_000;
const MAX_POSITION_PCT = 50;
const DEFAULT_TICKERS: Record<string, number> = {
  AAPL: 190,
  GOOGL: 175,
  MSFT: 420,
  AMZN: 185,
  TSLA: 250,
  NVDA: 800,
  META: 500,
  JPM: 195,
  V: 280,
  NFLX: 600,
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const round2 = (n: number) => Math.round(n * 100) / 100;

// --------------------------------------------------------------------------- prices

interface MockQuote {
  price: number;
  previous: number;
}
const quotes: Record<string, MockQuote> = {};

/** Unknown tickers get a deterministic pseudo-price so any symbol can be watched or traded. */
function ensureQuote(ticker: string): MockQuote {
  if (!quotes[ticker]) {
    const seed =
      DEFAULT_TICKERS[ticker] ??
      50 + ([...ticker].reduce((s, c) => s + c.charCodeAt(0), 0) % 250);
    quotes[ticker] = { price: seed, previous: seed };
  }
  return quotes[ticker];
}
Object.keys(DEFAULT_TICKERS).forEach(ensureQuote);

const priceOf = (ticker: string) => ensureQuote(ticker).price;

function toTick(ticker: string, q: MockQuote): PriceTick {
  const change = round2(q.price - q.previous);
  return {
    ticker,
    price: round2(q.price),
    previous_price: round2(q.previous),
    timestamp: Date.now() / 1000,
    change,
    change_percent: q.previous ? (change / q.previous) * 100 : 0,
    direction: change > 0 ? "up" : change < 0 ? "down" : "flat",
  };
}

/** Random-walk one step for every tracked ticker; returns the SSE-shaped payload. */
function stepPrices(): Record<string, PriceTick> {
  const out: Record<string, PriceTick> = {};
  for (const [ticker, q] of Object.entries(quotes)) {
    let move = (Math.random() - 0.5) * 0.004; // about +/-0.2% per tick
    if (Math.random() < 0.01) move += (Math.random() < 0.5 ? -1 : 1) * 0.03; // occasional "event"
    q.previous = q.price;
    q.price = Math.max(1, q.price * (1 + move));
    out[ticker] = toTick(ticker, q);
  }
  return out;
}

/** Stand-in for EventSource: emits the full price map every 500ms. Returns an unsubscribe fn. */
export function subscribeMockPrices(onData: (prices: Record<string, PriceTick>) => void): () => void {
  snapshotTimer ??= setInterval(recordSnapshot, 30_000);
  onData(Object.fromEntries(Object.entries(quotes).map(([t, q]) => [t, toTick(t, q)])));
  const id = setInterval(() => onData(stepPrices()), 500);
  return () => clearInterval(id);
}

// --------------------------------------------------------------------------- state

let cash = START_CASH;
const holdings = new Map<string, { quantity: number; avg_cost: number }>();
let realizedPnl = 0;
const watchlist: { ticker: string; added_by: string; added_at: string }[] = Object.keys(
  DEFAULT_TICKERS,
).map((ticker) => ({ ticker, added_by: "system", added_at: new Date().toISOString() }));

function buildPortfolio(): Portfolio {
  const positions: Position[] = [...holdings.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([ticker, h]) => {
      const price = priceOf(ticker);
      const market_value = h.quantity * price;
      const basis = h.quantity * h.avg_cost;
      return {
        ticker,
        quantity: h.quantity,
        avg_cost: h.avg_cost,
        current_price: price,
        market_value,
        unrealized_pnl: market_value - basis,
        pnl_percent: basis ? ((market_value - basis) / basis) * 100 : 0,
      };
    });
  const positions_value = positions.reduce((s, p) => s + p.market_value, 0);
  const total_value = cash + positions_value;
  return {
    cash_balance: cash,
    positions,
    positions_value,
    total_value,
    starting_cash: START_CASH,
    unrealized_pnl: positions.reduce((s, p) => s + p.unrealized_pnl, 0),
    realized_pnl: realizedPnl,
    total_pnl: total_value - START_CASH,
  };
}

// A short backfilled history so the P&L chart has a shape on first load.
const history: Snapshot[] = (() => {
  const out: Snapshot[] = [];
  const now = Date.now();
  let v = START_CASH;
  for (let i = 60; i > 0; i--) {
    v += (Math.random() - 0.5) * 12;
    out.push({
      total_value: round2(v),
      cash_balance: START_CASH,
      positions_value: 0,
      total_pnl: round2(v - START_CASH),
      recorded_at: new Date(now - i * 30_000).toISOString(),
    });
  }
  return out;
})();

function recordSnapshot() {
  const p = buildPortfolio();
  history.push({
    total_value: p.total_value,
    cash_balance: p.cash_balance,
    positions_value: p.positions_value,
    total_pnl: p.total_pnl,
    recorded_at: new Date().toISOString(),
  });
}
// Same cadence as the real backend's snapshot task. Started lazily (by subscribeMockPrices) so
// nothing runs at import time when the app talks to the real backend.
let snapshotTimer: ReturnType<typeof setInterval> | null = null;

// --------------------------------------------------------------------------- operations

function executeTrade(order: TradeOrder): TradeResult {
  const ticker = order.ticker.trim().toUpperCase();
  const price = round2(priceOf(ticker));
  const fail = (error: string): TradeResult => ({
    ok: false,
    trade_id: null,
    ticker,
    side: order.side,
    quantity: order.quantity,
    price: null,
    realized_pnl: null,
    error,
    warnings: [],
  });

  if (!(order.quantity > 0)) return fail("quantity must be a positive number");
  const amount = order.quantity * price;
  const held = holdings.get(ticker);
  let realized: number | null = null;

  if (order.side === "buy") {
    if (amount > cash + 1e-9)
      return fail(`insufficient cash: order costs $${amount.toFixed(2)}, available $${cash.toFixed(2)}`);
    const total = buildPortfolio().total_value;
    const weight = (((held?.quantity ?? 0) * price + amount) / total) * 100;
    if (weight > MAX_POSITION_PCT)
      return fail(`${ticker} would be ${weight.toFixed(1)}% of the portfolio (limit ${MAX_POSITION_PCT}%)`);
    const qty = (held?.quantity ?? 0) + order.quantity;
    holdings.set(ticker, {
      quantity: qty,
      avg_cost: ((held?.quantity ?? 0) * (held?.avg_cost ?? 0) + amount) / qty,
    });
    cash -= amount;
  } else {
    if (!held) return fail(`no ${ticker} position to sell`);
    if (order.quantity > held.quantity + 1e-9)
      return fail(`insufficient shares: selling ${order.quantity} ${ticker}, holding ${held.quantity}`);
    realized = (price - held.avg_cost) * order.quantity;
    realizedPnl += realized;
    cash += amount;
    const remaining = held.quantity - order.quantity;
    if (remaining < 1e-9) holdings.delete(ticker);
    else holdings.set(ticker, { ...held, quantity: remaining });
  }
  recordSnapshot();
  return {
    ok: true,
    trade_id: crypto.randomUUID(),
    ticker,
    side: order.side,
    quantity: order.quantity,
    price,
    realized_pnl: realized,
    error: null,
    warnings: [],
  };
}

function listWatchlist(): WatchlistEntry[] {
  return watchlist.map((w) => {
    const q = ensureQuote(w.ticker);
    const tick = toTick(w.ticker, q);
    return { ...w, price: tick.price, previous_price: tick.previous_price, change_percent: tick.change_percent, direction: tick.direction };
  });
}

function watchlistResult(action: string, ticker: string, error: string | null = null): WatchlistResult {
  return { ok: !error, ticker, action, error, note: null };
}

const CHAT_TRADE = /\b(buy|sell)\s+(\d+(?:\.\d+)?)\s+(?:shares?\s+of\s+)?([A-Za-z]{1,5})\b/gi;
const CHAT_WATCH = /\b(add|remove)\s+([A-Za-z]{1,5})\b/gi;

// --------------------------------------------------------------------------- the mock API

export const mockApi = {
  async portfolio(): Promise<Portfolio> {
    await sleep(120);
    return buildPortfolio();
  },

  async history(): Promise<Snapshot[]> {
    await sleep(120);
    return [...history];
  },

  async trade(order: TradeOrder): Promise<TradeResponse> {
    await sleep(200);
    const trade = executeTrade(order);
    if (!trade.ok) throw new ApiError(trade.error ?? "trade failed", 400, [trade.error ?? ""]);
    return { trade, portfolio: buildPortfolio() };
  },

  async watchlist(): Promise<WatchlistEntry[]> {
    await sleep(120);
    return listWatchlist();
  },

  async addTicker(raw: string): Promise<WatchlistResponse> {
    await sleep(150);
    const ticker = raw.trim().toUpperCase();
    if (!/^[A-Z]{1,5}(\.[A-Z])?$/.test(ticker)) throw new ApiError(`'${ticker}' is not a valid ticker symbol`, 400);
    if (!watchlist.some((w) => w.ticker === ticker)) {
      ensureQuote(ticker);
      watchlist.push({ ticker, added_by: "user", added_at: new Date().toISOString() });
    }
    return { result: watchlistResult("add", ticker), watchlist: listWatchlist() };
  },

  async removeTicker(raw: string): Promise<WatchlistResponse> {
    await sleep(150);
    const ticker = raw.trim().toUpperCase();
    const i = watchlist.findIndex((w) => w.ticker === ticker);
    if (i < 0) throw new ApiError(`${ticker} is not on the watchlist`, 404);
    watchlist.splice(i, 1);
    return { result: watchlistResult("remove", ticker), watchlist: listWatchlist() };
  },

  /** Deterministic stand-in for the LLM, like the backend's LLM_MOCK: parses "buy 5 AAPL" etc. */
  async chat(message: string): Promise<ChatResponse> {
    await sleep(700); // long enough to see the loading indicator
    const executed_trades = [...message.matchAll(CHAT_TRADE)].map(([, side, qty, ticker]) =>
      executeTrade({ ticker, side: side.toLowerCase() as "buy" | "sell", quantity: Number(qty) }),
    );
    const watchlist_changes: WatchlistResult[] = [];
    if (/watchlist/i.test(message)) {
      for (const [, action, raw] of message.matchAll(CHAT_WATCH)) {
        const ticker = raw.toUpperCase();
        if (action.toLowerCase() === "add") {
          ensureQuote(ticker);
          if (!watchlist.some((w) => w.ticker === ticker))
            watchlist.push({ ticker, added_by: "ai", added_at: new Date().toISOString() });
          watchlist_changes.push(watchlistResult("add", ticker));
        } else {
          const i = watchlist.findIndex((w) => w.ticker === ticker);
          if (i >= 0) watchlist.splice(i, 1);
          watchlist_changes.push(watchlistResult("remove", ticker, i < 0 ? `${ticker} is not on the watchlist` : null));
        }
      }
    }
    const acted = executed_trades.length > 0 || watchlist_changes.length > 0;
    const p = buildPortfolio();
    let text = acted
      ? "Mock mode: executing your request."
      : `Mock mode: your portfolio is worth $${p.total_value.toFixed(2)} with $${p.cash_balance.toFixed(2)} in cash. Try "buy 5 AAPL" or "add PYPL to my watchlist".`;
    const failures = executed_trades.filter((t) => !t.ok);
    if (failures.length) text += "\n\n" + failures.map((t) => `⚠ Could not ${t.side} ${t.quantity} ${t.ticker}: ${t.error}`).join("\n");
    return { message: text, executed_trades, watchlist_changes, warnings: [] };
  },

  async analysis(): Promise<AnalysisReport> {
    await sleep(120);
    const p = buildPortfolio();
    const positions = p.positions
      .map((x) => ({
        ticker: x.ticker,
        weight_pct: (x.market_value / p.total_value) * 100,
        unrealized_pnl: x.unrealized_pnl,
        pnl_pct: x.pnl_percent,
        day_change_pct: null,
      }))
      .sort((a, b) => b.weight_pct - a.weight_pct);
    return {
      total_value: p.total_value,
      cash_pct: (p.cash_balance / p.total_value) * 100,
      positions,
      top_concentration: positions[0] ?? null,
      herfindahl_index: positions.reduce((s, x) => s + (x.weight_pct / 100) ** 2, 0),
      best_performer: [...positions].sort((a, b) => b.unrealized_pnl - a.unrealized_pnl)[0] ?? null,
      worst_performer: [...positions].sort((a, b) => a.unrealized_pnl - b.unrealized_pnl)[0] ?? null,
      watchlist_movers: [],
      flags: positions.filter((x) => x.weight_pct > 25).map((x) => `${x.ticker} is ${x.weight_pct.toFixed(0)}% of the portfolio`),
    };
  },

  async riskAssessment(): Promise<RiskAssessment> {
    const a = await mockApi.analysis();
    const over = a.positions.filter((x) => x.weight_pct > 25).map((x) => x.ticker);
    const level = over.length ? (a.herfindahl_index > 0.5 ? "high" : "moderate") : "low";
    return {
      total_value: a.total_value,
      cash_pct: a.cash_pct,
      largest_position: a.top_concentration,
      herfindahl_index: a.herfindahl_index,
      positions_over_warn: over,
      positions_over_limit: a.positions.filter((x) => x.weight_pct > MAX_POSITION_PCT).map((x) => x.ticker),
      level,
      flags: a.flags,
    };
  },
};
